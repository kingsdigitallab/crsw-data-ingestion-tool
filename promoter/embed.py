"""Meaning-based search needs one vector per dataset. This module builds
them from the index rows and keeps them in the bucket as
`<index_prefix>/embeddings.parquet`, separate from the index proper so
the human-readable index stays lean and this file can be dropped or
rebuilt on its own (finding-and-reuse.md §5).

What is embedded is the record's title, abstract, subject terms and
source note, nothing from the file manifest and nothing from any file.
The work is incremental: a row whose identifier, modified stamp and
model match the previous file is kept, so a routine run embeds only
what changed. Amber records are embedded only when the configuration
says so (a Centre decision, default off); their rows are still written,
with no vector, so the file says the dataset exists."""
import io
import logging
from typing import Dict, List, Optional, Sequence

from botocore.exceptions import ClientError

try:                                    # optional, as in index.py
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover
    pa = pq = None

log = logging.getLogger("promoter.embed")

EMBEDDINGS_NAME = "embeddings.parquet"
COLUMNS = ("identifier", "dataset_uuid", "modified", "sensitivity",
           "model", "dimension", "embedding")
# The record fields whose text is embedded. Nothing else ever is.
TEXT_FIELDS = ("dataset", "abstract", "subject", "source_detail")


def text_for(row: Dict) -> str:
    """The text one dataset is embedded from: title, abstract, subject
    terms and source note, one per line, blanks left out."""
    parts = []
    for f in TEXT_FIELDS:
        v = row.get(f)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        if v:
            parts.append(str(v).strip())
    return "\n".join(parts)


def embeddings_key(prefix: str) -> str:
    return prefix.strip("/") + "/" + EMBEDDINGS_NAME


def read_existing(client, bucket: str, key: str) -> Dict[str, Dict]:
    """The previous file's rows by identifier; {} when there is none or
    pyarrow is absent."""
    if pq is None:
        return {}
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return {}
        raise
    table = pq.read_table(io.BytesIO(body))
    return {r["identifier"]: r for r in table.to_pylist() if r.get("identifier")}


def build(index_rows: Sequence[Dict], existing: Dict[str, Dict], platform,
          model: str, include_amber: bool) -> Dict:
    """Rows for the new file, and a summary of what was done. `platform`
    is anything with `embed(texts) -> vectors`."""
    out: List[Dict] = []
    todo: List[Dict] = []
    kept = skipped = 0
    for r in index_rows:
        ident = r.get("identifier")
        if not ident:
            continue
        base = {"identifier": ident, "dataset_uuid": r.get("dataset_uuid"),
                "modified": r.get("modified"), "sensitivity": r.get("sensitivity"),
                "model": model, "dimension": None, "embedding": None}
        if r.get("sensitivity") == "amber" and not include_amber:
            skipped += 1
            out.append(base)
            continue
        old = existing.get(ident)
        if (old and old.get("embedding") and old.get("model") == model
                and old.get("modified") == r.get("modified")):
            base["dimension"] = old.get("dimension") or len(old["embedding"])
            base["embedding"] = list(old["embedding"])
            kept += 1
            out.append(base)
            continue
        out.append(base)
        todo.append(base)
    if todo:
        vectors = platform.embed([text_for(_index_row(index_rows, t["identifier"]))
                                  for t in todo])
        for row_, vec in zip(todo, vectors):
            row_["embedding"] = vec
            row_["dimension"] = len(vec)
    dropped = len([i for i in existing if i not in {o["identifier"] for o in out}])
    dims = {o["dimension"] for o in out if o["dimension"]}
    return {"rows": out, "embedded": len(todo), "kept": kept, "skipped_amber": skipped,
            "dropped": dropped, "dimension": max(dims) if dims else None}


def _index_row(index_rows: Sequence[Dict], identifier: str) -> Dict:
    return next(r for r in index_rows if r.get("identifier") == identifier)


def parquet_bytes(rows: Sequence[Dict]) -> Optional[bytes]:
    if pa is None:
        return None
    schema = pa.schema([
        pa.field("identifier", pa.string()), pa.field("dataset_uuid", pa.string()),
        pa.field("modified", pa.string()), pa.field("sensitivity", pa.string()),
        pa.field("model", pa.string()), pa.field("dimension", pa.int64()),
        pa.field("embedding", pa.list_(pa.float32())),
    ])
    table = pa.Table.from_pylist([{c: r.get(c) for c in COLUMNS} for r in rows], schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def write(client, bucket: str, prefix: str, rows: Sequence[Dict]) -> Optional[str]:
    """Put the embeddings object; its key, or None when pyarrow is absent."""
    data = parquet_bytes(rows)
    if data is None:
        return None
    key = embeddings_key(prefix)
    client.put_object(Bucket=bucket, Key=key, Body=data,
                      ContentType="application/vnd.apache.parquet")
    return key


def refresh(client, bucket: str, prefix: str, index_rows: Sequence[Dict], platform,
            model: str, include_amber: bool) -> Dict:
    """Read the previous file, embed what changed, write the new one.
    Returns the summary from `build` plus the key written ('key' is None
    when pyarrow is absent, in which case nothing was embedded either)."""
    if pq is None:
        return {"key": None, "embedded": 0, "kept": 0, "skipped_amber": 0,
                "dropped": 0, "dimension": None}
    key = embeddings_key(prefix)
    result = build(index_rows, read_existing(client, bucket, key), platform,
                   model, include_amber)
    rows = result.pop("rows")
    result["key"] = write(client, bucket, prefix, rows)
    result["datasets"] = len(rows)
    return result
