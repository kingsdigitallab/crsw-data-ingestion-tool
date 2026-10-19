"""Datasets leave the store only through the promoter (r10, extending
r9's rule for categories): `python -m promoter remove`.

A dataset in place is every object version under its prefix: current
versions, noncurrent versions and delete markers alike, the bucket
being versioned. Removing it means listing exactly that prefix (with
its trailing slash, so `.../test` never reaches `.../test-dataset`),
deleting every version of every member, and the record last in a call
of its own after a fresh listing shows nothing else left: the mirror of
the write order, so a run that dies halfway leaves a prefix whose
record still says it is a dataset and the retry recognises it. An
abandoned staging deposit goes the same way with its control object
last. Nothing here reads or writes outside the one prefix it is given;
`__main__` logs each step and rebuilds the derived layers."""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from crsw_deposit import keys, record
from crsw_web.deposits import CONTROL_NAME, STATUS_COMPLETE

from .resolve import read_json_object

BATCH = 1000                 # delete_objects takes at most this many


class RemoveIncomplete(Exception):
    """Something was still under the prefix after the deletes."""

    def __init__(self, left: List[str]):
        self.left = left
        super().__init__("still in place: %s%s" % (
            ", ".join(left[:3]), " ..." if len(left) > 3 else ""))


@dataclass
class Listing:
    versions: List[Tuple[str, str]]       # (key, version id) of object versions
    markers: List[Tuple[str, str]]        # ... of delete markers
    bytes: int

    def keys(self) -> List[str]:
        return sorted({k for k, _ in self.versions + self.markers})

    def pairs(self) -> List[Tuple[str, str]]:
        return self.versions + self.markers

    def counts(self) -> Dict:
        return {"objects": len({k for k, _ in self.versions}),
                "versions": len(self.versions),
                "delete_markers": len(self.markers),
                "bytes": self.bytes}


@dataclass
class Target:
    prefix: str                          # with its trailing slash
    last_key: str                        # deleted last, in its own call
    problems: List[str] = field(default_factory=list)
    listing: Optional[Listing] = None
    detail: Dict = field(default_factory=dict)   # what the log line says


def list_versions(client, bucket: str, prefix: str) -> Listing:
    """Every version and delete marker under `prefix`, which must end
    with a slash."""
    if not prefix.endswith("/"):
        raise ValueError("prefix must end with a slash: %r" % prefix)
    versions, markers, size = [], [], 0
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []):
            versions.append((v["Key"], v["VersionId"]))
            size += int(v.get("Size") or 0)
        for m in page.get("DeleteMarkers", []):
            markers.append((m["Key"], m["VersionId"]))
    return Listing(versions, markers, size)


def inspect(client, bucket: str, identifier: str, index_rows: List[Dict]) -> Target:
    """Is this a dataset in place, and what is under it? Refuses at the
    first problem, making no further request: a malformed identifier
    before any request at all, then no record, a record that does not
    parse, or one whose identifier disagrees with its location (the
    typo guard). `index_rows` (promoter/index.py rows) give the count of
    datasets whose derived_from names this one; they are not rewritten."""
    t = Target(prefix=identifier + "/", last_key="")
    if not keys.dataset_prefix_ok(identifier):
        t.problems.append("not a dataset identifier "
                          "(strand/project/sensitivity/state/dataset)")
        return t
    t.last_key = identifier + "/" + keys.record_filename(identifier.rsplit("/", 1)[1])
    text, err = read_json_object(client, bucket, t.last_key)
    if err == "absent":
        t.problems.append("no record in place at %s" % t.last_key)
        return t
    if err:
        t.problems.append("record could not be read: %s" % err)
        return t
    try:
        rec = record.parse_record(text)
    except record.RecordParseError as e:
        t.problems.append("record does not parse: %s" % e)
        return t
    if rec.get("identifier") != identifier:
        t.problems.append("record identifier %r does not match its location %r"
                          % (rec.get("identifier"), identifier))
        return t
    t.listing = list_versions(client, bucket, t.prefix)
    referrers = sum(1 for r in index_rows
                    if identifier in (r.get("derived_from_identifiers") or []))
    t.detail = {"dataset_uuid": rec.get("dataset_uuid"), "created": rec.get("created"),
                "depositors": list(rec.get("depositors") or []),
                "referenced_by": referrers}
    t.detail.update(t.listing.counts())
    return t


def inspect_staging(client, bucket: str, staging_prefix: str, deposit_id: str) -> Target:
    """An abandoned deposit under staging/<user>/<id>/: the id is looked
    for under every user (the operator has the id from a log, not the
    user). One whose record was written, or whose control object says
    complete, is not abandoned: it is promoted or left to expire."""
    sp = staging_prefix.strip("/")
    t = Target(prefix="", last_key="")
    users, record_seen = set(), False
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=sp + "/"):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(sp) + 1:].split("/")
            if len(rel) >= 3 and rel[1] == deposit_id:
                users.add(rel[0])
                name = rel[-1]
                if name.startswith("dataset.") and name.endswith(".json"):
                    record_seen = True
    if not users:
        t.problems.append("no such deposit in staging under %s/" % sp)
        return t
    if len(users) > 1:
        t.problems.append("deposit id %s is under more than one user: %s"
                          % (deposit_id, ", ".join(sorted(users))))
        return t
    user = users.pop()
    root = "%s/%s/%s" % (sp, user, deposit_id)
    t.prefix, t.last_key = root + "/", root + "/" + CONTROL_NAME
    status = None
    text, _ = read_json_object(client, bucket, t.last_key)
    if text:
        try:
            status = json.loads(text).get("status")
        except (ValueError, AttributeError):
            status = "unreadable"
    if record_seen or status == STATUS_COMPLETE:
        t.problems.append("deposit has its record written: promote or let it expire")
        return t
    t.listing = list_versions(client, bucket, t.prefix)
    t.detail = {"user": user, "status": status}
    t.detail.update(t.listing.counts())
    return t


def _delete(client, bucket: str, pairs: List[Tuple[str, str]]) -> None:
    for i in range(0, len(pairs), BATCH):
        batch = pairs[i:i + BATCH]
        r = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k, "VersionId": v} for k, v in batch],
                    "Quiet": True})
        errors = r.get("Errors") or []
        if errors:
            first = errors[0]
            raise RuntimeError("%d of %d not deleted, e.g. %s: %s" % (
                len(errors), len(batch), first.get("Key"),
                first.get("Message") or first.get("Code")))


def remove(client, bucket: str, t: Target) -> Dict:
    """Everything but the last key, a fresh listing that must show only
    the last key, then every version of it, then a listing that must be
    empty. `RemoveIncomplete` names what is left; the last key is never
    touched while anything else remains."""
    if t.problems or t.listing is None:
        raise ValueError("target was refused or not inspected")
    _delete(client, bucket, [p for p in t.listing.pairs() if p[0] != t.last_key])
    left = list_versions(client, bucket, t.prefix)
    stray = [k for k in left.keys() if k != t.last_key]
    if stray:
        raise RemoveIncomplete(stray)
    _delete(client, bucket, left.pairs())
    left = list_versions(client, bucket, t.prefix)
    if left.keys():
        raise RemoveIncomplete(left.keys())
    return t.listing.counts()
