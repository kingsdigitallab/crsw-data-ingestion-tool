"""Meaning-based search needs one vector per dataset. This module builds
them from the index rows and keeps them in the bucket as
`<index_prefix>/embeddings.parquet`, separate from the index proper so
the human-readable index stays lean and this file can be dropped or
rebuilt on its own (finding-and-reuse.md §5).

What is embedded is the record's title, abstract, subject terms and
source note, nothing from the file manifest and nothing from any file.
The work is incremental: a row whose identifier, modified stamp and
model match the previous file is kept, so a routine run embeds only
what changed. Only records of the sensitivities the configuration
allows are sent to the platform (green until the Centre allows amber);
the other rows are still written, with no vector, so the file says the
dataset exists. Vectors are stored at half precision (16-bit floats),
which the ranking cannot tell apart from full precision."""
import io
import logging
from typing import Dict, List, Optional, Sequence, Set

from botocore.exceptions import ClientError

try:                                    # optional, as in index.py
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover
    np = pa = pq = None

log = logging.getLogger("promoter.embed")

EMBEDDINGS_NAME = "embeddings.parquet"
# `cut` is the vector length that was asked for (0 = the model's full
# length); a row is kept only when the same cut is still asked for.
COLUMNS = ("identifier", "dataset_uuid", "modified", "sensitivity",
           "model", "cut", "dimension", "embedding")
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
          model: str, sensitivities: Set[str], dims: Optional[int] = None) -> Dict:
    """Rows for the new file, and a summary of what was done. `platform`
    is anything with `embed(texts) -> vectors`. `dims` is the vector
    length in use; a kept row must have it, so a change re-embeds."""
    out: List[Dict] = []
    todo: List[Dict] = []
    kept = skipped = 0
    for r in index_rows:
        ident = r.get("identifier")
        if not ident:
            continue
        base = {"identifier": ident, "dataset_uuid": r.get("dataset_uuid"),
                "modified": r.get("modified"), "sensitivity": r.get("sensitivity"),
                "model": model, "cut": dims or 0, "dimension": None, "embedding": None}
        if r.get("sensitivity") not in sensitivities:
            skipped += 1
            out.append(base)
            continue
        old = existing.get(ident)
        if (old and old.get("embedding") and old.get("model") == model
                and old.get("modified") == r.get("modified")
                and (old.get("cut") or 0) == (dims or 0)):
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
    return {"rows": out, "embedded": len(todo), "kept": kept, "skipped": skipped,
            "dropped": dropped, "dimension": max(dims) if dims else None}


def _index_row(index_rows: Sequence[Dict], identifier: str) -> Dict:
    return next(r for r in index_rows if r.get("identifier") == identifier)


def parquet_bytes(rows: Sequence[Dict]) -> Optional[bytes]:
    if pa is None:
        return None
    schema = pa.schema([
        pa.field("identifier", pa.string()), pa.field("dataset_uuid", pa.string()),
        pa.field("modified", pa.string()), pa.field("sensitivity", pa.string()),
        pa.field("model", pa.string()), pa.field("cut", pa.int64()),
        pa.field("dimension", pa.int64()), pa.field("embedding", pa.list_(pa.float16())),
    ])
    # pyarrow takes 16-bit floats only from numpy, not from Python floats.
    vectors = pa.array([np.asarray(r["embedding"], dtype=np.float16)
                        if r.get("embedding") else None for r in rows],
                       type=pa.list_(pa.float16()))
    table = pa.Table.from_pylist([{c: r.get(c) for c in COLUMNS if c != "embedding"}
                                  for r in rows],
                                 schema=schema.remove(schema.get_field_index("embedding")))
    table = table.append_column("embedding", vectors)
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
            model: str, sensitivities: Set[str], dims: Optional[int] = None) -> Dict:
    """Read the previous file, embed what changed, write the new one.
    Returns the summary from `build` plus the key written ('key' is None
    when pyarrow is absent, in which case nothing was embedded either)."""
    if pq is None:
        return {"key": None, "embedded": 0, "kept": 0, "skipped": 0,
                "dropped": 0, "dimension": None}
    key = embeddings_key(prefix)
    result = build(index_rows, read_existing(client, bucket, key), platform,
                   model, sensitivities, dims)
    rows = result.pop("rows")
    result["key"] = write(client, bucket, prefix, rows)
    result["datasets"] = len(rows)
    return result
