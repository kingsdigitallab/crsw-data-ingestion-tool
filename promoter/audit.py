"""The audit trail: one object per promoter run in the bucket, and the
two review commands that read the bucket back.

Journald on the internal VM is size-capped and dies with the VM, so
each run's log lines are also written, once, as
`<audit_prefix>/YYYY/MM/DD/<run-id>.jsonl` with the key the promoter
already holds. The bucket is versioned, so the trail cannot be edited
in place. Nothing here changes a dataset."""
import json
import re
from typing import Dict, Iterable, Iterator, List, Optional

from crsw_deposit import keys, record

from .log import Log
from . import index as index_mod

_RUN_KEY_RE = re.compile(r"/(?P<date>\d{4}/\d{2}/\d{2})/(?P<run>[^/]+)\.jsonl$")


def audit_key(prefix: str, run_id: str) -> str:
    """`audit/promoter/2026/09/24/20260924T163000Z-ab12.jsonl`; the date
    comes from the run id, so one run is one object under one day."""
    d = run_id[:8]
    return "%s/%s/%s/%s/%s.jsonl" % (prefix.strip("/"), d[:4], d[4:6], d[6:8], run_id)


def write_run(client, bucket: str, prefix: str, log: Log) -> str:
    """Put this run's lines so far as one JSON-lines object; returns the key."""
    key = audit_key(prefix, log.run_id)
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in log.lines)
    client.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"),
                      ContentType="application/x-ndjson")
    return key


# --- reading the trail ---------------------------------------------------

def list_runs(client, bucket: str, prefix: str,
              since: Optional[str] = None) -> List[str]:
    """Audit object keys, newest first. `since` is YYYY-MM-DD; runs on
    or after that day only."""
    paginator = client.get_paginator("list_objects_v2")
    found = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.strip("/") + "/"):
        for obj in page.get("Contents", []):
            m = _RUN_KEY_RE.search(obj["Key"])
            if not m:
                continue
            day = m.group("date").replace("/", "-")
            if since and day < since:
                continue
            found.append(obj["Key"])
    return sorted(found, reverse=True)


def read_run(client, bucket: str, key: str) -> List[Dict]:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def matches(entry: Dict, dataset: Optional[str] = None, user: Optional[str] = None,
            action: Optional[str] = None) -> bool:
    if action and entry.get("action") != action:
        return False
    if user and user not in (entry.get("user"), entry.get("by")):
        return False
    if dataset:
        hay = [str(entry.get(k) or "") for k in
               ("prefix", "dataset_uuid", "destination", "record_key", "dataset")]
        if not any(dataset == h or dataset in h.split("/") or h.startswith(dataset + "/")
                   for h in hay):
            return False
    return True


def trail(client, bucket: str, prefix: str, runs: Optional[int] = 10,
          since: Optional[str] = None, **filters) -> Iterator[Dict]:
    """Entries from the newest `runs` runs (all runs when None or when
    `since` is given), each run oldest line first, filtered."""
    run_keys = list_runs(client, bucket, prefix, since=since)
    if since is None and runs is not None:
        run_keys = run_keys[:runs]
    for key in run_keys:
        for entry in read_run(client, bucket, key):
            if matches(entry, **filters):
                yield entry


human_bytes = record.human_bytes


_HEAD = ("when", "action", "deposit", "user", "by", "prefix", "run_id")


def describe(entry: Dict) -> str:
    """One readable line: time, action, what, who, then the rest."""
    when = str(entry.get("when", ""))[:16].replace("T", " ")
    action = entry.get("action", "?")
    parts = [when, action.ljust(20)]
    if entry.get("prefix"):
        parts.append(entry["prefix"])
    who = entry.get("user") or entry.get("by")
    if who:
        parts.append("by " + who)
    if entry.get("deposit"):
        parts.append("deposit " + str(entry["deposit"]))
    if entry.get("run_id"):
        parts.append("run " + str(entry["run_id"]))
    rest = []
    for k, v in entry.items():
        if k in _HEAD or v in (None, [], {}, ""):
            continue
        if k == "bytes":
            v = human_bytes(v)
        elif isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        rest.append("%s=%s" % (k, v))
    return "  ".join(parts + rest)


# --- what is in place ------------------------------------------------------

DATASET_COLUMNS = ("prefix", "identifier", "dataset_uuid", "depositor", "created",
                   "modified", "files", "bytes", "schema_version", "vocabulary_version")


def list_records(client, bucket: str, strand: Optional[str] = None) -> List[Dict]:
    """One row per dataset record in place, from the record and the
    object's depositor label."""
    rows = []
    for ref, rec, labels in index_mod.read_records(client, bucket, strand):
        files = rec.get("files") or []
        rows.append({
            "prefix": ref.prefix,
            "identifier": rec.get("identifier"),
            "dataset_uuid": rec.get("dataset_uuid"),
            "depositor": labels.get("depositor"),
            "created": rec.get("created"),
            "modified": rec.get("modified"),
            "files": len(files),
            "bytes": sum(int(f.get("bytes") or 0) for f in files if isinstance(f, dict)),
            "schema_version": rec.get("schema_version"),
            "vocabulary_version": rec.get("vocabulary_version"),
            "record_key": ref.record_key,
        })
    return rows


def table(rows: Iterable[Dict], columns=DATASET_COLUMNS) -> str:
    rows = list(rows)
    if not rows:
        return "(nothing in place)"
    cells = [[("" if r.get(c) is None else
               (human_bytes(r[c]) if c == "bytes" else str(r[c]))) for c in columns]
             for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths)).rstrip()]
    for row in cells:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def valid_strand(s: Optional[str]) -> Optional[str]:
    if s and s not in keys.STRANDS:
        raise ValueError("unknown strand %r; one of %s" % (s, ", ".join(keys.STRANDS)))
    return s
