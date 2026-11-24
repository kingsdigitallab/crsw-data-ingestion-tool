"""One vector per dataset, for search by meaning, kept in the bucket as
`<index_prefix>/embeddings.parquet`.

What is embedded is the record's title, abstract, subject terms and
source note: nothing from the file manifest and nothing from any file.
With the vocabulary to hand, a subject term is sent as its label and
scope note, never as the bare slug. Only records of the sensitivities
the configuration allows are sent to the platform; the other rows are
written with no vector, so the file says the dataset exists.

The work is incremental: a row whose identifier, modified stamp, model
and `text_sha` match the previous file is kept. `model` is the
fingerprint of what the embed alias resolves to on the platform, so a
backend swapped behind an unchanged alias re-embeds every row. Vectors
are stored at half precision (16-bit floats)."""
import hashlib
import io
import logging
from typing import Dict, List, Optional, Sequence, Set

from botocore.exceptions import ClientError

from crsw_deposit import authority

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
           "model", "cut", "dimension", "text_sha", "embedding")
# The record fields whose text is embedded. Nothing else ever is.
TEXT_FIELDS = ("dataset", "abstract", "subject", "source_detail")


def text_for(row: Dict, vocab_doc: Optional[Dict] = None) -> str:
    """Return the text one dataset is embedded from: title, abstract,
    subject terms and source note, one per line, blanks left out. With a
    term authority file each subject is its label and scope note on a
    line of its own ("Label: scope"; the label alone without a note; the
    slug only when the vocabulary does not know it). Without one, the
    slugs."""
    parts = []
    for f in TEXT_FIELDS:
        v = row.get(f)
        if f == "subject" and isinstance(v, list) and vocab_doc is not None \
                and authority.is_authority(vocab_doc):
            parts.extend(_subject_lines(v, vocab_doc))
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        if v:
            parts.append(str(v).strip())
    return "\n".join(parts)


def _subject_lines(subjects: Sequence, vocab_doc: Dict) -> List[str]:
    labels = authority.labels_of(vocab_doc)
    scopes = authority.scopes_of(vocab_doc)
    out = []
    for s in subjects:
        if not s:
            continue
        line = labels.get(s, str(s))
        if scopes.get(s):
            line += ": " + scopes[s]
        out.append(line)
    return out


def text_sha(text: str) -> str:
    """Return sixteen hex characters that name the text a vector was made
    from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def stale_text(index_rows: Sequence[Dict], existing: Dict[str, Dict],
               vocab_doc: Optional[Dict] = None) -> List[str]:
    """Return the identifiers whose vector in `existing` was made from
    other text than the row gives today. Rows without a vector are not
    counted."""
    stale = []
    for r in index_rows:
        old = existing.get(r.get("identifier"))
        if old and old.get("embedding") and old.get("text_sha") != text_sha(text_for(r, vocab_doc)):
            stale.append(r["identifier"])
    return stale


def missing_rows(index_rows: Sequence[Dict], existing: Dict[str, Dict]) -> List[str]:
    """Return the identifiers in the index with no row at all in
    `existing`. A row without a vector is not missing."""
    return [r["identifier"] for r in index_rows
            if r.get("identifier") and r["identifier"] not in existing]


def embeddings_key(prefix: str) -> str:
    return prefix.strip("/") + "/" + EMBEDDINGS_NAME


def read_existing(client, bucket: str, key: str) -> Dict[str, Dict]:
    """Return the previous file's rows by identifier; {} when there is none
    or pyarrow is absent."""
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


def models_in(existing: Dict[str, Dict]) -> List[str]:
    """Return the distinct `model` values of the rows that carry a vector,
    sorted."""
    return sorted({r.get("model") for r in existing.values()
                   if r.get("embedding") and r.get("model")})


def build(index_rows: Sequence[Dict], existing: Dict[str, Dict], platform,
          model: str, sensitivities: Set[str], dims: Optional[int] = None,
          vocab_doc: Optional[Dict] = None) -> Dict:
    """Build the rows for the new file and a summary of what was done, and
    return them. `platform` is anything with `embed(texts) -> vectors`;
    `model` is the resolved fingerprint the vectors are keyed to and
    `dims` the vector length in use. A previous row is kept only if it
    matches on model, dims and `text_sha`. `vocab_doc` is the vocabulary
    whose labels and scope notes go into the text (see `text_for`)."""
    out: List[Dict] = []
    todo: List[Dict] = []
    texts: Dict[str, str] = {}
    kept = skipped = 0
    for r in index_rows:
        ident = r.get("identifier")
        if not ident:
            continue
        base = {"identifier": ident, "dataset_uuid": r.get("dataset_uuid"),
                "modified": r.get("modified"), "sensitivity": r.get("sensitivity"),
                "model": model, "cut": dims or 0, "dimension": None,
                "text_sha": None, "embedding": None}
        if r.get("sensitivity") not in sensitivities:
            skipped += 1
            out.append(base)
            continue
        texts[ident] = text_for(r, vocab_doc)
        base["text_sha"] = text_sha(texts[ident])
        old = existing.get(ident)
        # A kept row must match on model, modified stamp, dims and text.
        if (old and old.get("embedding") and old.get("model") == model
                and old.get("modified") == r.get("modified")
                and (old.get("cut") or 0) == (dims or 0)
                and old.get("text_sha") == base["text_sha"]):
            base["dimension"] = old.get("dimension") or len(old["embedding"])
            base["embedding"] = list(old["embedding"])
            kept += 1
            out.append(base)
            continue
        out.append(base)
        todo.append(base)
    if todo:
        vectors = platform.embed([texts[t["identifier"]] for t in todo])
        for row_, vec in zip(todo, vectors):
            row_["embedding"] = vec
            row_["dimension"] = len(vec)
    dropped = len([i for i in existing if i not in {o["identifier"] for o in out}])
    dims = {o["dimension"] for o in out if o["dimension"]}
    return {"rows": out, "embedded": len(todo), "kept": kept, "skipped": skipped,
            "dropped": dropped, "dimension": max(dims) if dims else None}


def parquet_bytes(rows: Sequence[Dict]) -> Optional[bytes]:
    if pa is None:
        return None
    schema = pa.schema([
        pa.field("identifier", pa.string()), pa.field("dataset_uuid", pa.string()),
        pa.field("modified", pa.string()), pa.field("sensitivity", pa.string()),
        pa.field("model", pa.string()), pa.field("cut", pa.int64()),
        pa.field("dimension", pa.int64()), pa.field("text_sha", pa.string()),
        pa.field("embedding", pa.list_(pa.float16())),
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
    """Put the embeddings object and return its key, or None when pyarrow
    is absent."""
    data = parquet_bytes(rows)
    if data is None:
        return None
    key = embeddings_key(prefix)
    client.put_object(Bucket=bucket, Key=key, Body=data,
                      ContentType="application/vnd.apache.parquet")
    return key


def refresh(client, bucket: str, prefix: str, index_rows: Sequence[Dict], platform,
            model: str, sensitivities: Set[str], dims: Optional[int] = None,
            existing: Optional[Dict[str, Dict]] = None,
            vocab_doc: Optional[Dict] = None) -> Dict:
    """Read the previous file (or take it as `existing`, already read),
    embed what changed, write the new file and return the summary from
    `build` plus the key written. 'key' is None when pyarrow is absent,
    in which case nothing was embedded either."""
    if pq is None:
        return {"key": None, "embedded": 0, "kept": 0, "skipped": 0,
                "dropped": 0, "dimension": None}
    key = embeddings_key(prefix)
    if existing is None:
        existing = read_existing(client, bucket, key)
    result = build(index_rows, existing, platform, model, sensitivities, dims, vocab_doc)
    rows = result.pop("rows")
    result["key"] = write(client, bucket, prefix, rows)
    result["datasets"] = len(rows)
    return result
