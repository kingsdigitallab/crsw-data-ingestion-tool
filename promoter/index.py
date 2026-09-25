"""The index: one row per dataset record in place, written to the bucket
after every run that changed something, in two forms.

`<index_prefix>/datasets.jsonl` (one JSON object per line) and
`<index_prefix>/datasets.parquet` (the same rows; readable straight from
S3 by DuckDB, cdisaw-parquet, a laptop). The bucket is the truth and the
records are the source; the index is a cache anything can rebuild with
`python -m promoter index`. Nothing here changes a record.

Parquet needs pyarrow, which is in the `web` extra the image installs;
without it the JSON lines are still written and the Parquet is skipped
with a log line."""
import io
import json
from typing import Dict, Iterator, List, Optional, Tuple

from crsw_deposit import record

from .scan import DatasetRef, list_datasets

try:                                    # optional: the Parquet copy
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover - exercised on a bare CLI
    pa = pq = None

JSONL_NAME = "datasets.jsonl"
PARQUET_NAME = "datasets.parquet"

# Column order of the index. Lists stay lists; derived_from and
# provenance are kept whole as JSON text so the schema stays flat.
COLUMNS = (
    "identifier", "dataset_uuid", "strand", "project", "sensitivity", "state",
    "dataset", "domain", "abstract", "subject", "temporal_start", "temporal_end",
    "version", "creator", "license", "source_type", "source_detail", "steward",
    "depositor", "depositors", "created", "modified", "files", "bytes",
    "schema_version", "vocabulary_version", "derived_from",
    "derived_from_identifiers", "provenance", "provenance_tools", "origin",
    "record_key", "indexed_at",
)
LIST_COLUMNS = ("subject", "depositors", "derived_from_identifiers", "provenance_tools")
INT_COLUMNS = ("files", "bytes")


def read_records(client, bucket: str, strand: Optional[str] = None
                 ) -> Iterator[Tuple[DatasetRef, Dict, Dict]]:
    """(ref, record, labels) for every dataset record in place, the
    record upgraded to the current schema on read (so a 0.5 string
    derived_from is a reference like any other). An unreadable record
    yields an empty dict so the row still says the prefix exists."""
    for ref in list_datasets(client, bucket):
        if strand and not ref.prefix.startswith(strand + "/"):
            continue
        obj = client.get_object(Bucket=bucket, Key=ref.record_key)
        try:
            rec = json.loads(obj["Body"].read().decode("utf-8"))
            rec, _ = record.upgrade_record(rec)
        except (ValueError, record.RecordParseError, AttributeError):
            rec = {}
        yield ref, rec, dict(obj.get("Metadata") or {})


def row(ref: DatasetRef, rec: Dict, labels: Dict, indexed_at: str) -> Dict:
    files = [f for f in (rec.get("files") or []) if isinstance(f, dict)]
    start, end = record.temporal_pair(rec.get("temporal"))
    refs = rec.get("derived_from") if isinstance(rec.get("derived_from"), list) else []
    acts = rec.get("provenance") if isinstance(rec.get("provenance"), list) else []
    tools = []
    for a in acts:
        name = ((a.get("tool") or {}) if isinstance(a, dict) else {}).get("name")
        if name and name not in tools:
            tools.append(name)
    return {
        "identifier": rec.get("identifier") or ref.prefix,
        "dataset_uuid": rec.get("dataset_uuid"),
        "strand": rec.get("strand"), "project": rec.get("project"),
        "sensitivity": rec.get("sensitivity"), "state": rec.get("state"),
        "dataset": rec.get("dataset"), "domain": rec.get("domain"),
        "abstract": rec.get("abstract"),
        "subject": list(rec.get("subject") or []),
        "temporal_start": start, "temporal_end": end,
        "version": rec.get("version"), "creator": rec.get("creator"),
        "license": rec.get("license"), "source_type": rec.get("source_type"),
        "source_detail": rec.get("source_detail"), "steward": rec.get("steward"),
        "depositor": labels.get("depositor"),
        "depositors": list(rec.get("depositors") or []),
        "created": rec.get("created"), "modified": rec.get("modified"),
        "files": len(files),
        "bytes": sum(int(f.get("bytes") or 0) for f in files),
        "schema_version": rec.get("schema_version"),
        "vocabulary_version": rec.get("vocabulary_version"),
        "derived_from": json.dumps(refs, ensure_ascii=False) if refs else None,
        "derived_from_identifiers": [r.get("identifier") for r in refs
                                     if isinstance(r, dict) and r.get("kind") == "dataset"
                                     and r.get("identifier")],
        "provenance": json.dumps(acts, ensure_ascii=False) if acts else None,
        "provenance_tools": tools,
        # Where the dataset came from, as far as the record says: the
        # first tool that produced it (cdisaw-parquet names itself), else
        # a plain deposit. Lets a reader keep a harmonised corpus apart.
        "origin": tools[0] if tools else "deposit",
        "record_key": ref.record_key,
        "indexed_at": indexed_at,
    }


def rows(client, bucket: str, strand: Optional[str] = None,
         now: Optional[str] = None) -> List[Dict]:
    now = now or record.utc_now_iso()
    return [row(ref, rec, labels, now)
            for ref, rec, labels in read_records(client, bucket, strand)]


def jsonl_bytes(rows_: List[Dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows_).encode("utf-8")


def parquet_bytes(rows_: List[Dict]) -> Optional[bytes]:
    """The same rows as Parquet, or None when pyarrow is not installed."""
    if pa is None:
        return None
    fields = []
    for c in COLUMNS:
        if c in LIST_COLUMNS:
            fields.append(pa.field(c, pa.list_(pa.string())))
        elif c in INT_COLUMNS:
            fields.append(pa.field(c, pa.int64()))
        else:
            fields.append(pa.field(c, pa.string()))
    schema = pa.schema(fields)
    table = pa.Table.from_pylist([{c: r.get(c) for c in COLUMNS} for r in rows_],
                                 schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def write(client, bucket: str, prefix: str, rows_: List[Dict]) -> Dict[str, Optional[str]]:
    """Put both index objects. Returns {'jsonl': key, 'parquet': key or
    None (pyarrow absent)}."""
    prefix = prefix.strip("/")
    keys_ = {"jsonl": prefix + "/" + JSONL_NAME, "parquet": None}
    client.put_object(Bucket=bucket, Key=keys_["jsonl"], Body=jsonl_bytes(rows_),
                      ContentType="application/x-ndjson")
    data = parquet_bytes(rows_)
    if data is not None:
        keys_["parquet"] = prefix + "/" + PARQUET_NAME
        client.put_object(Bucket=bucket, Key=keys_["parquet"], Body=data,
                          ContentType="application/vnd.apache.parquet")
    return keys_
