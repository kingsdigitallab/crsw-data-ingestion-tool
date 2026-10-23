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
import re
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
    before any request at all, then no record (current or hidden under
    a delete marker), a record that does not parse, or one whose
    identifier disagrees with its location (the typo guard). `index_rows` (promoter/index.py rows) give the count of
    datasets whose derived_from names this one; they are not rewritten."""
    t = Target(prefix=identifier + "/", last_key="")
    if not keys.dataset_prefix_ok(identifier):
        t.problems.append("not a dataset identifier "
                          "(strand/project/sensitivity/state/dataset)")
        return t
    t.last_key = identifier + "/" + keys.record_filename(identifier.rsplit("/", 1)[1])
    t.listing = list_versions(client, bucket, t.prefix)
    text, err = read_json_object(client, bucket, t.last_key)
    hidden = False
    if err == "absent":
        # Deleted by hand on the versioned bucket: a marker on top, the
        # record's versions beneath. The newest still says what the
        # prefix is (S3 lists a key's versions newest first).
        vid = next((v for k, v in t.listing.versions if k == t.last_key), None)
        if vid is None:
            t.problems.append("no record in place at %s" % t.last_key)
            return t
        try:
            text = client.get_object(Bucket=bucket, Key=t.last_key, VersionId=vid
                                     )["Body"].read().decode("utf-8")
        except Exception as e:
            t.problems.append("record version %s could not be read: %s" % (vid, e))
            return t
        hidden = True
    elif err:
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
    referrers = sum(1 for r in index_rows
                    if identifier in (r.get("derived_from_identifiers") or []))
    t.detail = {"dataset_uuid": rec.get("dataset_uuid"), "created": rec.get("created"),
                "depositors": list(rec.get("depositors") or []),
                "referenced_by": referrers, "record_hidden": hidden}
    t.detail.update(t.listing.counts())
    return t


# r12: the refusals (promoter.checks.check wording, and cmd_remove's
# "control object missing") that are the deposit's own doing, so that
# removing it is the fix: a member whose stored bytes or size are not the
# manifest's, a missing or mislabelled object, a record that is absent,
# unreadable or disagrees with the control object, and a deposit that has
# its record written but a control object that never reached complete
# (an upload that died between the two: nothing will finish it). Any
# other refusal (a record invalid against the vocabulary the promoter
# happened to fetch, authorisation, size caps, the destination record)
# may be the VM's configuration, and the deposit would promote once that
# is right: such a deposit is never removed here.
CONTENT_REFUSALS = (r"stored bytes hash to", r"size mismatch at ", r"no object at ",
                    r": label ", r"staged record's files manifest differs",
                    r"staged record identifier ", r"staged record dataset_uuid",
                    r"staged record cannot be used", r"^staged record \S*/\S*: ",
                    r"^record_key ", r"^member path", r"^deposit status is ",
                    r"^deposit has no record_key", r"^deposit has no files",
                    r"^control object missing or unreadable")


def deposit_at_fault(problems: List[str]) -> bool:
    """True when every one of the promoter's problems with a deposit is
    about its own content (see CONTENT_REFUSALS)."""
    return bool(problems) and all(any(re.search(m, p) for m in CONTENT_REFUSALS)
                                  for p in problems)


def inspect_staging(client, bucket: str, staging_prefix: str, deposit_id: str,
                    refusal=None) -> Target:
    """An abandoned or refused deposit under staging/<user>/<id>/: the
    id is looked for under every user (the operator has the id from a
    log, not the user). One whose record was written, or whose control
    object says complete, is finished: it may go only when the
    promoter's own checks refuse it (r12), which `refusal(user, id)`
    answers with the problems (an empty list: it passes, so promote it).
    Without `refusal` a finished deposit is never removed here."""
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
    refused_because = None
    if record_seen or status == STATUS_COMPLETE:
        refused_because = refusal(user, deposit_id) if refusal else []
        if not refused_because:
            t.problems.append("deposit has its record written and passes the promoter's "
                              "checks: promote it" if refusal else
                              "deposit has its record written: promote or let it expire")
            return t
        if not deposit_at_fault(refused_because):
            t.problems.append(
                "deposit has its record written and the promoter refuses it, but not "
                "for its own bytes or record: the cause may be the vocabulary fetched, "
                "the promoter's authorisation or limits, or the destination record. "
                "Fix that and run the promoter; it is removed here only when its own "
                "content is wrong. Refused because: %s" % "; ".join(refused_because))
            return t
    t.listing = list_versions(client, bucket, t.prefix)
    t.detail = {"user": user, "status": status}
    if refused_because:
        t.detail["refused_because"] = refused_because
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
