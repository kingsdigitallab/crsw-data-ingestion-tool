"""Searching inside documents (finding-and-reuse.md §7): the text of each
text-bearing file in a dataset, split into passages and embedded, kept in
the bucket as `<index_prefix>/passages/<identifier>.parquet`.

Kept under the index prefix rather than beside the record, so a dataset
prefix holds only what its depositor put there and the web VM's
read-scoped key over `index/` covers it. Like the rest of the index it
is a cache: rebuildable from the files with `promoter passages`, never
the truth.

What is read: plain text, Markdown, PDF (text layer only; a page with no
text is counted, not OCRed) and Word. Tables (CSV, spreadsheets) and
everything else are left alone. Incremental by the file's checksum from
the record, so a re-deposit only re-embeds files that changed. Only
datasets of the sensitivities the configuration allows are touched at
all, and the run stops at a per-dataset passage cap so one mistaken
deposit cannot fill the VM."""
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from botocore.exceptions import ClientError

try:                                    # optional, as in index.py
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover
    np = pa = pq = None

log = logging.getLogger("promoter.passages")

PASSAGE_WORDS = 350      # about a page of a transcript
OVERLAP_WORDS = 50       # so a sentence cut at a boundary is whole in one of them
TEXT_TYPES = (".txt", ".md", ".markdown", ".text", ".rst")
PDF_TYPE = ".pdf"
WORD_TYPE = ".docx"
COLUMNS = ("identifier", "dataset_uuid", "sensitivity", "member", "checksum", "page",
           "position", "words", "text", "model", "cut", "dimension", "partial", "embedding")

Page = Tuple[Optional[int], str]      # (page number from 1, or None; its text)
# A file that was read and had no text (an image-only PDF) leaves one row
# with this position and no vector, so its checksum is remembered and the
# file is not fetched again on every run. Readers must skip such rows.
NO_TEXT = -1


def passages_key(prefix: str, identifier: str) -> str:
    return prefix.strip("/") + "/passages/" + identifier.strip("/") + ".parquet"


def supported(member: str) -> bool:
    return member.lower().endswith(TEXT_TYPES + (PDF_TYPE, WORD_TYPE))


# --- text out of files ------------------------------------------------------------

def extract(member: str, data: bytes) -> Optional[List[Page]]:
    """Pages of text, or None when the file type is not one this reads.
    A supported file with no text at all gives []."""
    name = member.lower()
    if name.endswith(TEXT_TYPES):
        text = data.decode("utf-8", errors="replace")
        return [(None, text)] if text.strip() else []
    if name.endswith(PDF_TYPE):
        return _pdf_pages(data)
    if name.endswith(WORD_TYPE):
        return _docx_pages(data)
    return None


def _pdf_pages(data: bytes) -> List[Page]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as e:                          # not a PDF this can open
        log.warning("PDF unreadable: %s", e)
        return []
    out: List[Page] = []
    for i, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:                      # one bad page, not the file
            log.warning("page %d unreadable: %s", i, e)
            text = ""
        if text.strip():
            out.append((i, text))
    return out


def _docx_pages(data: bytes) -> List[Page]:
    import docx
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:                          # not a Word file this can open
        log.warning("Word file unreadable: %s", e)
        return []
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    return [(None, text)] if text.strip() else []


# --- passages ---------------------------------------------------------------------

_WS = re.compile(r"\s+")


def chunk(pages: Sequence[Page], words: int = PASSAGE_WORDS,
          overlap: int = OVERLAP_WORDS) -> List[Tuple[Optional[int], int, str]]:
    """(page, position, text) passages of about `words` words, each
    starting `overlap` words before the previous one ended. The page is
    the one the passage starts on. Whitespace is normalised, nothing
    else is changed."""
    tagged: List[Tuple[Optional[int], str]] = []
    for page, text in pages:
        for w in _WS.split(text.strip()):
            if w:
                tagged.append((page, w))
    if not tagged:
        return []
    step = max(1, words - overlap)
    out = []
    start = 0
    position = 0
    while start < len(tagged):
        piece = tagged[start:start + words]
        out.append((piece[0][0], position, " ".join(w for _, w in piece)))
        position += 1
        if start + words >= len(tagged):
            break
        start += step
    return out


@dataclass
class Summary:
    identifier: str
    files: int = 0               # members in the record
    files_read: int = 0          # text-bearing and read
    files_kept: int = 0          # unchanged since last time, not re-read
    files_other: int = 0         # types this does not read
    files_too_big: int = 0       # over the size cap
    files_no_text: int = 0       # supported type, nothing to extract (image-only PDF)
    passages: int = 0
    embedded: int = 0
    kept: int = 0
    dropped: int = 0             # passages of members no longer in the record
    capped: bool = False         # the per-dataset passage cap stopped the walk
    dimension: Optional[int] = None
    key: Optional[str] = None

    def as_dict(self) -> Dict:
        return dict(self.__dict__)


def build(identifier: str, rec: Dict, fetch: Callable[[str], bytes],
          existing: Sequence[Dict], platform, model: str, dims: Optional[int],
          max_file_bytes: int, max_passages: int) -> Tuple[List[Dict], Summary]:
    """Rows for the dataset's passages file and a summary. `fetch(member)`
    returns a member's bytes; it is called only for files that changed."""
    s = Summary(identifier=identifier)
    by_member: Dict[str, List[Dict]] = {}
    for r in existing:
        by_member.setdefault(r.get("member") or "", []).append(r)
    rows: List[Dict] = []
    todo: List[Dict] = []
    in_record: Set[str] = set()
    n = 0                     # passages so far, against the cap
    base = {"identifier": identifier, "dataset_uuid": rec.get("dataset_uuid"),
            "sensitivity": rec.get("sensitivity"), "model": model, "cut": dims or 0}
    for entry in rec.get("files") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        member, checksum = entry["path"], entry.get("checksum_sha256")
        in_record.add(member)
        s.files += 1
        if not supported(member):
            s.files_other += 1
            continue
        if int(entry.get("bytes") or 0) > max_file_bytes:
            s.files_too_big += 1
            continue
        room = max_passages - n
        if room <= 0:
            s.capped = True   # the walk stops here: nothing further is fetched
            break
        old = by_member.get(member) or []
        same = bool(old) and all(o.get("checksum") == checksum and o.get("model") == model
                                 and (o.get("cut") or 0) == (dims or 0) for o in old)
        old_text = sorted((o for o in old if o.get("position") != NO_TEXT),
                          key=lambda o: o.get("position") or 0)
        complete = same and all(o.get("embedding") for o in old_text)
        was_partial = any(o.get("partial") for o in old)
        # A file cut short by the cap before is read again only when there
        # is now room for more of it than it holds.
        if complete and (not was_partial or len(old_text) >= room):
            kept = [dict(o) for o in old_text[:room]]
            if was_partial or len(kept) < len(old_text):
                for o in kept:
                    o["partial"] = True
                s.capped = True
            rows.extend(dict(o) for o in old if o.get("position") == NO_TEXT)
            rows.extend(kept)
            n += len(kept)
            s.files_kept += 1
            s.kept += len(kept)
            if s.capped:
                break
            continue
        # A passage whose text was embedded before, with this model and
        # cut, keeps its vector: a file that grew re-embeds only what is new.
        reuse = {o["text"]: o for o in old_text
                 if o.get("embedding") and o.get("text") and o.get("model") == model
                 and (o.get("cut") or 0) == (dims or 0)}
        pages = extract(member, fetch(member))
        if pages is None:
            s.files_other += 1
            continue
        s.files_read += 1
        pieces = chunk(pages)
        if not pieces:
            rows.append(dict(base, member=member, checksum=checksum, page=None,
                             position=NO_TEXT, words=0, text=None, dimension=None,
                             embedding=None))
            continue
        mine: List[Dict] = []
        for page, position, text in pieces:
            if n >= max_passages:
                s.capped = True
                break
            prev = reuse.get(text)
            if prev is not None:
                row = dict(prev, checksum=checksum, page=page, position=position,
                           partial=False)
                rows.append(row)
                s.kept += 1
            else:
                row = dict(base, member=member, checksum=checksum, page=page,
                           position=position, words=len(text.split()), text=text,
                           dimension=None, embedding=None, partial=False)
                todo.append(row)
            mine.append(row)
            n += 1
        if s.capped:
            for row in mine:
                row["partial"] = True     # read again once the cap leaves room
            break
    if todo:
        vectors = platform.embed([t["text"] for t in todo])
        for row, vec in zip(todo, vectors):
            row["embedding"], row["dimension"] = vec, len(vec)
        rows.extend(todo)
    s.embedded = len(todo)
    s.dropped = sum(1 for m, v in by_member.items() if m not in in_record
                    for o in v if o.get("position") != NO_TEXT)
    s.files_no_text = sum(1 for r in rows if r["position"] == NO_TEXT)
    s.passages = len(rows) - s.files_no_text
    dimensions = {r["dimension"] for r in rows if r.get("dimension")}
    s.dimension = max(dimensions) if dimensions else None
    rows.sort(key=lambda r: (r["member"], r["position"]))
    return rows, s


# --- the file in the bucket -------------------------------------------------------

def read_existing(client, bucket: str, key: str) -> List[Dict]:
    if pq is None:
        return []
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return []
        raise
    return pq.read_table(io.BytesIO(body)).to_pylist()


def parquet_bytes(rows: Sequence[Dict]) -> Optional[bytes]:
    if pa is None:
        return None
    schema = pa.schema([
        pa.field("identifier", pa.string()), pa.field("dataset_uuid", pa.string()),
        pa.field("sensitivity", pa.string()), pa.field("member", pa.string()),
        pa.field("checksum", pa.string()), pa.field("page", pa.int64()),
        pa.field("position", pa.int64()), pa.field("words", pa.int64()),
        pa.field("text", pa.string()), pa.field("model", pa.string()),
        pa.field("cut", pa.int64()), pa.field("dimension", pa.int64()),
        pa.field("partial", pa.bool_()),
    ])
    table = pa.Table.from_pylist([{c: r.get(c) for c in COLUMNS if c != "embedding"}
                                  for r in rows], schema=schema)
    vectors = pa.array([np.asarray(r["embedding"], dtype=np.float16)
                        if r.get("embedding") else None for r in rows],
                       type=pa.list_(pa.float16()))
    table = table.append_column("embedding", vectors)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def refresh(client, bucket: str, prefix: str, identifier: str, rec: Dict, platform,
            model: str, dims: Optional[int], max_file_bytes: int,
            max_passages: int) -> Summary:
    """Read the previous file, re-read and embed what changed, write the
    new file (or remove it when the dataset has no text). Members are
    fetched from the store with the promoter's own client."""
    key = passages_key(prefix, identifier)
    if pq is None:
        s = Summary(identifier=identifier)
        return s

    def fetch(member: str) -> bytes:
        return client.get_object(Bucket=bucket, Key=identifier + "/" + member)["Body"].read()

    existing = read_existing(client, bucket, key)
    rows, s = build(identifier, rec, fetch, existing, platform, model, dims,
                    max_file_bytes, max_passages)
    if rows:
        client.put_object(Bucket=bucket, Key=key, Body=parquet_bytes(rows),
                          ContentType="application/vnd.apache.parquet")
        s.key = key
    elif existing:
        client.delete_object(Bucket=bucket, Key=key)
    return s


def listing(client, bucket: str, prefix: str, under: str = "") -> Dict[str, str]:
    """identifier -> key of every passages file in the bucket, or of
    those whose identifier starts with `under` (a strand or a dataset)."""
    base = prefix.strip("/") + "/passages/"
    out: Dict[str, str] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                 Prefix=base + under):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".parquet"):
                out[key[len(base):-len(".parquet")]] = key
    return out


def remove(client, bucket: str, prefix: str, identifier: str) -> Optional[str]:
    """Delete a dataset's passages file, for when its passages are no
    longer permitted (excluded, or its sensitivity taken off the list).
    The key when there was one to delete, else None."""
    key = passages_key(prefix, identifier)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code", "") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    client.delete_object(Bucket=bucket, Key=key)
    return key


def excluded(identifier: str, patterns: Sequence[str]) -> bool:
    """An identifier, or any prefix of one ending at a path element,
    named in PROMOTER_PASSAGES_EXCLUDE."""
    return any(identifier == p or identifier.startswith(p.rstrip("/") + "/")
               for p in patterns if p)
