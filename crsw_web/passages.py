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
        self._con = duckdb.connect(path)
        self._con.execute("CREATE TABLE IF NOT EXISTS files (key VARCHAR PRIMARY KEY, etag VARCHAR)")
        self._con.execute("CREATE TABLE IF NOT EXISTS meta (name VARCHAR PRIMARY KEY, value VARCHAR)")
        stored = self._con.execute("SELECT value FROM meta WHERE name = 'dims'").fetchone()
        if stored:
            self.dims = int(stored[0])
        if self.dims:
            self._ensure_table()

    # --- the table -----------------------------------------------------------------

    def _ensure_table(self) -> None:
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS passages ("
            "identifier VARCHAR, dataset_uuid VARCHAR, sensitivity VARCHAR, member VARCHAR, "
            "checksum VARCHAR, page INTEGER, position INTEGER, words INTEGER, text VARCHAR, "
            "dimension INTEGER, embedding FLOAT[%d], key VARCHAR)" % self.dims)
        self._con.execute("INSERT OR REPLACE INTO meta VALUES ('dims', ?)", [str(self.dims)])

    def _has_table(self) -> bool:
        return bool(self._con.execute(
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
        known = dict(self._con.execute("SELECT key, etag FROM files").fetchall())
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
                continue
            self._con.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", [key, etag])
            summary["loaded"] += 1 if n else 0
        self.error = None
        self._synced_at = self._clock()
        if summary["loaded"] or summary["dropped"]:
            log.info("passages synced: %s", summary)
        return summary

    def _drop(self, key: str) -> None:
        if self._has_table():
            self._con.execute("DELETE FROM passages WHERE key = ?", [key])
        self._con.execute("DELETE FROM files WHERE key = ?", [key])

    def _load(self, key: str, body: bytes) -> int:
        table = pq.read_table(io.BytesIO(body))
        # Rows with a vector only: the promoter's "no text" markers stay behind.
        mask = pa.compute.and_(pa.compute.is_valid(table.column("embedding")),
                               pa.compute.greater_equal(table.column("position"), 0))
        table = table.filter(mask)
        self._drop(key)
        if table.num_rows == 0:
            return 0
        dims = int(pa.compute.max(table.column("dimension")).as_py() or 0)
        if not self.dims:
            self.dims = dims
            self._ensure_table()
        if dims != self.dims:
            raise ValueError("vectors of %d numbers; this copy holds %d (rebuild by deleting %s)"
                             % (dims, self.dims, self.path))
        i = table.schema.get_field_index("embedding")
        table = table.set_column(i, "embedding",
                                 table.column("embedding").cast(pa.list_(pa.float32())))
        self._con.register("incoming", table)
        try:
            self._con.execute(
                "INSERT INTO passages SELECT %s, embedding::FLOAT[%d], ? FROM incoming"
                % (", ".join(COLUMNS), self.dims), [key])
        finally:
            self._con.unregister("incoming")
        return table.num_rows

    # --- search ----------------------------------------------------------------------

    def search(self, vector: Sequence[float], limit: int = 20,
               sensitivities: Optional[Sequence[str]] = None) -> List[Dict]:
        """Closest passages, best first, each a dict with a `score`."""
        self._maybe_sync()
        if not self.dims or not self._has_table():
            return []
        vec = [float(x) for x in vector]
        if len(vec) != self.dims:
            return []
        where, params = "", [vec]
        if sensitivities is not None:
            sens = list(sensitivities) or [""]
            where = " WHERE sensitivity IN (%s)" % ", ".join("?" for _ in sens)
            params += sens
        params.append(int(limit))
        rows = self._con.execute(
            "SELECT identifier, dataset_uuid, sensitivity, member, page, position, words, text, "
            "array_cosine_similarity(embedding, ?::FLOAT[%d]) AS score FROM passages%s "
            "ORDER BY score DESC LIMIT ?" % (self.dims, where), params).fetchall()
        names = ("identifier", "dataset_uuid", "sensitivity", "member", "page", "position",
                 "words", "text", "score")
        return [dict(zip(names, r)) for r in rows]

    def stats(self) -> Dict:
        self._maybe_sync()
        if not self._has_table():
            return {"passages": 0, "datasets": 0, "files": 0}
        passages, datasets, files = self._con.execute(
            "SELECT count(*), count(DISTINCT identifier), count(DISTINCT member || identifier) "
            "FROM passages").fetchone()
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
