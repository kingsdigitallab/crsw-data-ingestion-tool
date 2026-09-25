"""Resolve `derived_from` references against the store (r8 §4).

A web deposit cannot see destinations, so a `dataset` reference arrives
with only the identifier the researcher typed. The promoter holds the
real key: it reads the parent's record and fills in `dataset_uuid` and
`version`, which are what a catalogue keys on. A parent that is not
there is a warning, never a block: a researcher may deposit a final
dataset before its interim sibling. Nothing here writes anything."""
from typing import Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from crsw_deposit import keys, record


def read_json_object(client, bucket: str, key: str):
    """(text, None) or (None, 'absent') or (None, error)."""
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None, "absent"
        return None, code or str(e)
    return obj["Body"].read().decode("utf-8"), None


def parent_record_key(identifier: str) -> str:
    return identifier + "/" + keys.record_filename(identifier.rsplit("/", 1)[1])


def lookup(client, bucket: str, refs: Optional[List[Dict]]
           ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Fill in what the store knows about each `dataset` reference.

    Returns (references, resolved, unresolved): the references with
    `dataset_uuid` and `version` filled in where they were missing; one
    entry per reference that was filled in (index, identifier,
    dataset_uuid, version); one per reference that could not be
    (index, identifier, reason). References that already carry both
    values, and external references, pass through untouched."""
    out, resolved, unresolved = [], [], []
    for i, ref in enumerate(refs or []):
        ref = dict(ref) if isinstance(ref, dict) else ref
        out.append(ref)
        if not isinstance(ref, dict) or ref.get("kind") != "dataset":
            continue
        if ref.get("dataset_uuid") and ref.get("version"):
            continue
        identifier = str(ref.get("identifier") or "")
        if identifier.count("/") != 4:
            continue                      # validate_record reports the shape
        text, err = read_json_object(client, bucket, parent_record_key(identifier))
        if text is None:
            unresolved.append({"index": i + 1, "identifier": identifier,
                               "reason": err})
            continue
        try:
            parent = record.parse_record(text)
        except record.RecordParseError as e:
            unresolved.append({"index": i + 1, "identifier": identifier,
                               "reason": "parent record unreadable: %s" % e})
            continue
        if not ref.get("dataset_uuid") and parent.get("dataset_uuid"):
            ref["dataset_uuid"] = parent["dataset_uuid"]
        if not ref.get("version") and parent.get("version"):
            ref["version"] = parent["version"]
        resolved.append({"index": i + 1, "identifier": identifier,
                         "dataset_uuid": ref.get("dataset_uuid"),
                         "version": ref.get("version")})
    return out, resolved, unresolved
