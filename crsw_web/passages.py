"""The VM's copy of the passages the promoter wrote (finding-and-reuse.md
§7), arranged for nearest-neighbour search.

The bucket holds `<index_prefix>/passages/<identifier>.parquet`, one file
per dataset, and is the durable copy. This keeps the same rows in one
DuckDB file on local disk (or in memory when no path is writable) and
refreshes it on the index cycle: one listing, then only files whose ETag
changed are fetched, and files gone from the bucket are dropped. Delete
the DuckDB file and it is rebuilt from the bucket on the next request.
Rows the promoter marked as "read, no text" carry no vector and are
never loaded.

Search is a cosine similarity over every row with DuckDB's fixed-size
array functions: no extension to install, so no egress the VM may not
have. That is milliseconds at a hundred thousand passages and around a
second at a million; DuckDB's vss index is the step up if it is ever
needed. Access is not decided here: the caller filters what it shows by
the same rule as downloads."""
import io
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

from botocore.exceptions import ClientError

try:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover
    duckdb = pa = pq = None

log = logging.getLogger("crsw.passages")

COLUMNS = ("identifier", "dataset_uuid", "sensitivity", "member", "checksum", "page",
           "position", "words", "text", "dimension")

# --- saying why a passage matched -------------------------------------------------
# A vector cannot say which words matched; it can say which sentence of a
# passage is closest to the question. The sentences are embedded (one call
# for every passage shown) and the closest is marked on the page.

# A break after . ? or ! (a closing quote or bracket may follow it) when a
# capital, digit, quote or bracket starts the next sentence, or at a line
# break. Python lookbehinds are fixed-width, hence the two alternatives.
_SENTENCE_END = re.compile(
    r"(?:(?<=[.?!])|(?<=[.?!][\"')\]]))\s+(?=[A-Z0-9\"'(\[])|\n+")
_STEM = 5     # a question word matches on its first five letters
# Question words that say nothing about the passage.
_STOP = frozenset("""that this with from have were what when which where about into than
then they them their does been being will would could should there these those
also only some such very much more most over under between after before because
while during through your ours mine each every other others same both either
neither whether whose whom here just like make made many used using data""".split())


def split_sentences(text: str) -> List[str]:
    """Sentences of a passage, plain: a break after . ? or ! when a
    capital, digit or quote follows, or at a line break. Never empty."""
    parts = [p.strip() for p in _SENTENCE_END.split(text or "") if p and p.strip()]
    return parts or [(text or "").strip()]


def mark_closest(vectors: Sequence[Sequence[float]], question: Sequence[float],
                 margin: float = 0.03, at_most: int = 2) -> List[bool]:
    """Which sentences to mark: the one closest to the question, and the
    next within `margin` of it so a two-sentence idea is not cut in half,
    never more than `at_most` (a highlight of half a passage is none)."""
    import numpy as np
    if not vectors:
        return []
    m = np.asarray(vectors, dtype=np.float32)
    q = np.asarray(question, dtype=np.float32)
    if m.ndim != 2 or m.shape[1] != q.shape[0]:
        return [False] * len(vectors)
    norms = np.linalg.norm(m, axis=1); norms[norms == 0] = 1.0
    scores = (m @ q) / norms / (float(np.linalg.norm(q)) or 1.0)
    best = float(scores.max())
    chosen = [i for i in np.argsort(-scores)[:at_most] if scores[i] >= best - margin]
    return [i in chosen for i in range(len(vectors))]


def question_stems(question: str) -> List[str]:
    """The question's telling words, cut to their first five letters, so
    "verified" and "verification" are the same stem. Stop words and
    anything under four letters are left out."""
    stems = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", question or ""):
        w = w.lower().strip("'-")
        if len(w) >= 4 and w not in _STOP and w[:_STEM] not in stems:
            stems.append(w[:_STEM])
    return stems


def word_pattern(question: str) -> Optional["re.Pattern"]:
    """A regex for the question's words as they appear in a passage
    (question_stems, each continued to the end of the word)."""
    stems = question_stems(question)
    if not stems:
        return None
    alts = sorted(stems, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(s) for s in alts) + r")[a-z0-9'-]*",
                      re.IGNORECASE)


class PassageIndex:
    def __init__(self, client, bucket: str, index_prefix: str = "index",
                 path: str = ":memory:", refresh_seconds: int = 60,
                 dims: Optional[int] = None, clock: Callable[[], float] = time.monotonic):
        if duckdb is None:
            raise RuntimeError("duckdb and pyarrow are needed for passage search")
        self._client = client
        self._bucket = bucket
        self._prefix = index_prefix.strip("/") + "/passages/"
        self.path = path
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._synced_at: Optional[float] = None
        self.error: Optional[str] = None
        self.dims = dims or None
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # DuckDB connections are not thread-safe: the parent only hands out
        # cursors (under _cursors), sync writes through its own, and each
        # read takes a fresh one, so a reader sees a file whole or not yet.
        self._con = duckdb.connect(path)
        self._cursors = threading.Lock()
        self._w = self._cursor()
        self._w.execute("CREATE TABLE IF NOT EXISTS files (key VARCHAR PRIMARY KEY, etag VARCHAR)")
        self._w.execute("CREATE TABLE IF NOT EXISTS meta (name VARCHAR PRIMARY KEY, value VARCHAR)")
        stored = self._w.execute("SELECT value FROM meta WHERE name = 'dims'").fetchone()
        if stored and self.dims and int(stored[0]) != self.dims:
            # The configured size wins: the copy is a cache of the bucket,
            # so it is emptied and refilled at the new size.
            log.info("passages copy holds vectors of %s numbers, configured %d: rebuilding",
                     stored[0], self.dims)
            self._w.execute("DROP TABLE IF EXISTS passages")
            self._w.execute("DELETE FROM files")
        elif stored:
            self.dims = int(stored[0])
        if self.dims:
            self._ensure_table()

    # --- the table -----------------------------------------------------------------

    def _cursor(self):
        with self._cursors:
            return self._con.cursor()

    def _ensure_table(self) -> None:
        self._w.execute(
            "CREATE TABLE IF NOT EXISTS passages ("
            "identifier VARCHAR, dataset_uuid VARCHAR, sensitivity VARCHAR, member VARCHAR, "
            "checksum VARCHAR, page INTEGER, position INTEGER, words INTEGER, text VARCHAR, "
            "dimension INTEGER, embedding FLOAT[%d], key VARCHAR)" % self.dims)
        self._w.execute("INSERT OR REPLACE INTO meta VALUES ('dims', ?)", [str(self.dims)])

    def _has_table(self, con=None) -> bool:
        return bool((con or self._w).execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'passages'").fetchone())

    # --- keeping in step with the bucket -----------------------------------------------

    def stale(self) -> bool:
        return (self._synced_at is None or
                (self.refresh_seconds > 0 and
                 self._clock() - self._synced_at >= self.refresh_seconds))

    def _maybe_sync(self) -> None:
        if not self.stale():
            return
        with self._lock:
            if self.stale():
                self.sync()

    def _listing(self) -> Dict[str, str]:
        out = {}
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".parquet"):
                    out[obj["Key"]] = obj.get("ETag") or ""
        return out

    def sync(self) -> Dict[str, int]:
        """Fetch what changed, drop what went. Returns counts. A failure
        leaves what was loaded before and sets `error`."""
        summary = {"loaded": 0, "dropped": 0, "unchanged": 0, "skipped": 0}
        try:
            wanted = self._listing()
        except Exception as e:
            self.error = "passages listing failed: %s" % e
            self._synced_at = self._clock()
            return summary
        known = dict(self._w.execute("SELECT key, etag FROM files").fetchall())
        last_problem = ""
        for key in set(known) - set(wanted):
            self._drop(key)
            summary["dropped"] += 1
        for key, etag in wanted.items():
            if known.get(key) == etag:
                summary["unchanged"] += 1
                continue
            try:
                body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
                n = self._load(key, body)
            except Exception as e:
                log.warning("passages file %s not loaded: %s", key, e)
                summary["skipped"] += 1
                last_problem = str(e)
                continue
            self._w.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", [key, etag])
            summary["loaded"] += 1 if n else 0
        self.error = ("%d passages files not loaded (%s)" % (summary["skipped"], last_problem)
                      if summary["skipped"] else None)
        self._synced_at = self._clock()
        if summary["loaded"] or summary["dropped"]:
            log.info("passages synced: %s", summary)
        return summary

    def _drop(self, key: str) -> None:
        if self._has_table():
            self._w.execute("DELETE FROM passages WHERE key = ?", [key])
        self._w.execute("DELETE FROM files WHERE key = ?", [key])

    def _load(self, key: str, body: bytes) -> int:
        table = pq.read_table(io.BytesIO(body))
        # Rows with a vector only: the promoter's "no text" markers stay behind.
        mask = pa.compute.and_(pa.compute.is_valid(table.column("embedding")),
                               pa.compute.greater_equal(table.column("position"), 0))
        table = table.filter(mask)
        self._w.execute("BEGIN TRANSACTION")
        try:
            n = self._replace(key, table)
        except BaseException:
            self._w.execute("ROLLBACK")
            if not self._has_table():
                self.dims = None      # the table this load created went with it
            raise
        self._w.execute("COMMIT")
        return n

    def _replace(self, key: str, table) -> int:
        self._drop(key)
        if table.num_rows == 0:
            return 0
        dims = int(pa.compute.max(table.column("dimension")).as_py() or 0)
        if not self.dims:
            self.dims = dims
            self._ensure_table()
        if dims != self.dims:
            raise ValueError("vectors of %d numbers, configured %d: rewrite them with "
                             "the promoter at the configured size" % (dims, self.dims))
        i = table.schema.get_field_index("embedding")
        table = table.set_column(i, "embedding",
                                 table.column("embedding").cast(pa.list_(pa.float32())))
        self._w.register("incoming", table)
        try:
            self._w.execute(
                "INSERT INTO passages SELECT %s, embedding::FLOAT[%d], ? FROM incoming"
                % (", ".join(COLUMNS), self.dims), [key])
        finally:
            self._w.unregister("incoming")
        return table.num_rows

    # --- search ----------------------------------------------------------------------

    MIN_WORDS = 6    # a shorter passage is shown only when it contains the words asked

    def search(self, vector: Sequence[float], limit: int = 20,
               sensitivities: Optional[Sequence[str]] = None,
               phrase: str = "", stems: Sequence[str] = (),
               by_meaning: bool = False) -> List[Dict]:
        """Passages, best first, each a dict with a `score` (cosine) and a
        `why`: "phrase" when the passage contains the words as typed,
        "words" when it contains every telling word of the question, else
        "meaning". Phrase hits come first, then words, then the rest by
        meaning, so a passage someone has seen and types back is found.
        A passage under MIN_WORDS is noise to a vector and is shown only
        on a phrase or words match. With `by_meaning` the order is by
        similarity alone (each still says why)."""
        self._maybe_sync()
        con = self._cursor()
        try:
            return self._search(con, vector, limit, sensitivities, phrase, stems, by_meaning)
        finally:
            con.close()

    def _search(self, con, vector, limit, sensitivities, phrase, stems,
                by_meaning) -> List[Dict]:
        if not self.dims or not self._has_table(con):
            return []
        vec = [float(x) for x in vector]
        if len(vec) != self.dims:
            return []
        params: List = [vec]
        phrase = " ".join((phrase or "").split())
        if phrase:
            # Literal: \ % and _ escaped, so an identifier such as
            # case_ref_12 matches itself and nothing else.
            phrase_sql = "text ILIKE ? ESCAPE '\\'"
            literal = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append("%" + literal + "%")
        else:
            phrase_sql = "FALSE"
        if stems:
            words_sql = " AND ".join("regexp_matches(text, ?)" for _ in stems)
            params.extend(r"(?i)\b" + re.escape(s) for s in stems)
        else:
            words_sql = "FALSE"
        where = " WHERE (words >= %d OR phrase OR allwords)" % self.MIN_WORDS
        if sensitivities is not None:
            sens = list(sensitivities) or [""]
            where += " AND sensitivity IN (%s)" % ", ".join("?" for _ in sens)
            params += sens
        params.append(int(limit))
        rows = con.execute(
            "SELECT * FROM (SELECT identifier, dataset_uuid, sensitivity, member, page, position, "
            "words, text, array_cosine_similarity(embedding, ?::FLOAT[%d]) AS score, "
            "(%s) AS phrase, (%s) AS allwords FROM passages)%s "
            "ORDER BY %s LIMIT ?"
            % (self.dims, phrase_sql, words_sql, where,
               "score DESC" if by_meaning else "phrase DESC, allwords DESC, score DESC"),
            params).fetchall()
        names = ("identifier", "dataset_uuid", "sensitivity", "member", "page", "position",
                 "words", "text", "score", "phrase", "allwords")
        out = []
        for r in rows:
            d = dict(zip(names, r))
            d["why"] = "phrase" if d.pop("phrase") else ("words" if d.pop("allwords", False) else "meaning")
            d.pop("allwords", None)
            out.append(d)
        return out

    def get(self, identifier: str, member: str, position: int) -> Optional[Dict]:
        """One passage as stored, or None."""
        self._maybe_sync()
        con = self._cursor()
        try:
            if not self._has_table(con):
                return None
            row = con.execute(
                "SELECT identifier, sensitivity, member, page, position, text FROM passages "
                "WHERE identifier = ? AND member = ? AND position = ?",
                [identifier, member, int(position)]).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return dict(zip(("identifier", "sensitivity", "member", "page", "position", "text"), row))

    def stats(self) -> Dict:
        self._maybe_sync()
        con = self._cursor()
        try:
            if not self._has_table(con):
                return {"passages": 0, "datasets": 0, "files": 0}
            passages, datasets, files = con.execute(
                "SELECT count(*), count(DISTINCT identifier), count(DISTINCT member || identifier) "
                "FROM passages").fetchone()
        finally:
            con.close()
        return {"passages": passages, "datasets": datasets, "files": files}


def open_index(client, bucket: str, index_prefix: str, path: str, refresh_seconds: int,
               dims: Optional[int]) -> "PassageIndex":
    """A PassageIndex at `path`, creating its directory; in memory, with a
    warning, when the path cannot be used (a read-only container without
    the volume). In memory the copy is rebuilt at every start."""
    if path and path != ":memory:":
        try:
            return PassageIndex(client, bucket, index_prefix, path, refresh_seconds, dims)
        except Exception as e:
            log.warning("passages copy cannot use %s (%s); keeping it in memory", path, e)
    return PassageIndex(client, bucket, index_prefix, ":memory:", refresh_seconds, dims)
