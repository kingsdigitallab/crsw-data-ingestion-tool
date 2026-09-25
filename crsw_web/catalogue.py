"""What is in the store, read through the read-only key.

The promoter writes `<index_prefix>/datasets.jsonl`, one row per dataset
record in place (promoter/index.py). This cache reads it once, re-reads
at most once per `refresh_seconds`, and keeps the rows in hand when a
re-read fails, so the find page never goes blank because of one bad
request. Records and members are read live, never cached: the record
is the truth and the index only says where to look."""
import json
import threading
import time
from typing import Callable, Dict, List, Optional

from botocore.exceptions import ClientError

from crsw_deposit import keys, record

INDEX_NAME = "datasets.jsonl"
SEARCH_FIELDS = ("identifier", "dataset", "project", "abstract", "creator",
                 "source_detail", "depositor")


class Catalogue:
    def __init__(self, client, bucket: str, index_prefix: str = "index",
                 refresh_seconds: int = 60,
                 clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._bucket = bucket
        self._index_key = index_prefix.strip("/") + "/" + INDEX_NAME
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: Optional[List[Dict]] = None
        self._loaded_at: Optional[float] = None
        self.error: Optional[str] = None

    # --- the index ------------------------------------------------------

    def _load(self) -> None:
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=self._index_key)["Body"].read()
            rows = [json.loads(line) for line in body.decode("utf-8").splitlines()
                    if line.strip()]
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            self.error = ("no index at %s yet (the promoter writes it after its "
                          "first promotion)" % self._index_key
                          if code in ("NoSuchKey", "404", "NotFound") else str(e))
            if self._rows is None:
                self._rows = []
        except Exception as e:
            self.error = str(e)
            if self._rows is None:
                self._rows = []
        else:
            self.error = None
            self._rows = sorted(rows, key=lambda r: r.get("identifier") or "")
        self._loaded_at = self._clock()

    def stale(self) -> bool:
        return (self._loaded_at is None or
                (self.refresh_seconds > 0 and
                 self._clock() - self._loaded_at >= self.refresh_seconds))

    def _maybe_load(self) -> None:
        if not self.stale():
            return
        with self._lock:
            if self.stale():
                self._load()

    def rows(self) -> List[Dict]:
        self._maybe_load()
        return list(self._rows or [])

    def built_at(self) -> Optional[str]:
        rows = self.rows()
        return rows[0].get("indexed_at") if rows else None

    def get(self, identifier: str) -> Optional[Dict]:
        for r in self.rows():
            if r.get("identifier") == identifier:
                return r
        return None

    def search(self, q: Optional[str] = None, strand: Optional[str] = None,
               state: Optional[str] = None, sensitivity: Optional[str] = None,
               subject: Optional[str] = None, project: Optional[str] = None,
               limit: Optional[int] = None) -> List[Dict]:
        """Case-insensitive substring match over the descriptive fields;
        the other arguments are exact filters. Newest modified first."""
        needle = (q or "").strip().lower()
        out = []
        for r in self.rows():
            if strand and r.get("strand") != strand:
                continue
            if state and r.get("state") != state:
                continue
            if sensitivity and r.get("sensitivity") != sensitivity:
                continue
            if project and r.get("project") != project:
                continue
            if subject and subject not in (r.get("subject") or []):
                continue
            if needle:
                hay = " ".join(str(r.get(f) or "") for f in SEARCH_FIELDS)
                hay += " " + " ".join(r.get("subject") or [])
                if needle not in hay.lower():
                    continue
            out.append(r)
        out.sort(key=lambda r: r.get("modified") or "", reverse=True)
        return out[:limit] if limit else out

    def subjects(self) -> List[str]:
        seen = set()
        for r in self.rows():
            seen.update(r.get("subject") or [])
        return sorted(seen)

    # --- live reads -----------------------------------------------------

    def record_key(self, identifier: str) -> str:
        return identifier + "/" + keys.record_filename(identifier.rsplit("/", 1)[1])

    def record_text(self, identifier: str) -> Optional[str]:
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self.record_key(identifier))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        return obj["Body"].read().decode("utf-8")

    def record(self, identifier: str) -> Optional[Dict]:
        text = self.record_text(identifier)
        if text is None:
            return None
        try:
            return record.parse_record(text)
        except record.RecordParseError:
            return None

    @staticmethod
    def member_key(identifier: str, path: str) -> str:
        return identifier + "/" + path
