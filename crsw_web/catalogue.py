"""What is in the store, read through the read-only key.

The promoter writes `<index_prefix>/datasets.jsonl`, one row per dataset
record in place (promoter/index.py). This cache reads it once, re-reads
at most once per `refresh_seconds`, and keeps the rows in hand when a
re-read fails, so the find page never goes blank because of one bad
request. Records and members are read live, never cached: the record
is the truth and the index only says where to look.

Beside the index the promoter may write `embeddings.parquet` (one
vector per dataset, promoter/embed.py). When it is there, `ask` ranks
datasets by how close their vector is to the question's, after the
question has been turned into ordinary filters (finding-and-reuse.md
§5). Every step of `ask` degrades on its own: no vectors, or the
platform down, and the answer is the ordinary search with a notice."""
import io
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from botocore.exceptions import ClientError

from crsw_deposit import keys, record

from .llm import PlatformError

try:                                    # ranking needs both; optional
    import numpy as np
    import pyarrow.parquet as pq
except ImportError:                      # pragma: no cover
    np = pq = None

log = logging.getLogger("crsw.catalogue")

INDEX_NAME = "datasets.jsonl"
EMBEDDINGS_NAME = "embeddings.parquet"
SEARCH_FIELDS = ("identifier", "dataset", "project", "abstract", "creator",
                 "source_detail", "depositor")
RERANK_TOP = 20          # how many candidates the reranker reads
FILTER_LABELS = (("strand", "strand"), ("state", "state"), ("sensitivity", "sensitivity"),
                 ("subject", "subject"), ("project", "project"))


@dataclass
class Answer:
    """What `ask` found: the rows in order, the filter the question was
    turned into, which steps ran, and a notice when one could not."""
    rows: List[Dict]
    filter: Dict[str, str] = field(default_factory=dict)
    steps: List[str] = field(default_factory=list)
    notice: Optional[str] = None

    def understood(self) -> str:
        return describe_filter(self.filter)


def describe_filter(filt: Dict[str, str]) -> str:
    """The filter in words, for the "Understood as" line."""
    parts = [label + " " + filt[k] for k, label in FILTER_LABELS if filt.get(k)]
    y0, y1 = filt.get("year_from"), filt.get("year_to")
    if y0 and y1:
        parts.append("years %s to %s" % (y0, y1))
    elif y0:
        parts.append("from %s" % y0)
    elif y1:
        parts.append("up to %s" % y1)
    if filt.get("words"):
        parts.append("words: " + filt["words"])
    return ", ".join(parts) if parts else "no filters; matching on the question itself"


def _year(value) -> Optional[int]:
    s = str(value or "")[:4]
    return int(s) if s.isdigit() else None


def years_overlap(row: Dict, y_from: Optional[str], y_to: Optional[str]) -> bool:
    """False only when the row's coverage clearly misses the asked range;
    a row with no dates is kept."""
    start, end = _year(row.get("temporal_start")), _year(row.get("temporal_end"))
    lo, hi = _year(y_from), _year(y_to)
    if lo is not None and end is not None and end < lo:
        return False
    if hi is not None and start is not None and start > hi:
        return False
    return True


def _hay(row: Dict) -> str:
    hay = " ".join(str(row.get(f) or "") for f in SEARCH_FIELDS)
    return (hay + " " + " ".join(row.get("subject") or [])).lower()


def name_match(row: Dict, question: str) -> bool:
    """A word of the question (four letters or more) inside the dataset's
    name: someone typing a name wants that dataset, whatever a model
    thinks of its abstract."""
    name = (row.get("dataset") or "").lower()
    return any(t in name for t in question.lower().split() if len(t) >= 4)


def words_match(row: Dict, words: str, all_of: bool) -> bool:
    """`all_of`: every word present (a phrase the model chose); otherwise
    any word of four letters or more (the raw question, forgivingly)."""
    hay = _hay(row)
    tokens = [t for t in words.lower().split() if t]
    if not all_of:
        tokens = [t for t in tokens if len(t) >= 4]
    if not tokens:
        return False
    return all(t in hay for t in tokens) if all_of else any(t in hay for t in tokens)


class Catalogue:
    def __init__(self, client, bucket: str, index_prefix: str = "index",
                 refresh_seconds: int = 60,
                 clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._bucket = bucket
        self._index_key = index_prefix.strip("/") + "/" + INDEX_NAME
        self._embed_key = index_prefix.strip("/") + "/" + EMBEDDINGS_NAME
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: Optional[List[Dict]] = None
        self._loaded_at: Optional[float] = None
        self.error: Optional[str] = None
        # (identifiers, unit rows one per identifier), replaced whole so a
        # ranking in another thread never sees one without the other.
        self._vectors: Tuple[List[str], object] = ([], None)

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
        self._load_vectors()
        self._loaded_at = self._clock()

    def _load_vectors(self) -> None:
        """The embeddings file, into one unit-normalised matrix. Missing
        or unreadable means no ranking by meaning, nothing worse."""
        self._vectors = self._read_vectors()

    def _read_vectors(self) -> Tuple[List[str], object]:
        if np is None or pq is None:
            return [], None
        try:
            body = self._client.get_object(Bucket=self._bucket, Key=self._embed_key)["Body"].read()
            ids, vecs = [], []
            for r in pq.read_table(io.BytesIO(body)).to_pylist():
                if r.get("identifier") and r.get("embedding"):
                    ids.append(r["identifier"])
                    vecs.append(r["embedding"])
            if not ids:
                return [], None
            m = np.asarray(vecs, dtype=np.float32)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return ids, m / norms
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404", "NotFound"):
                log.warning("embeddings unreadable at %s: %s", self._embed_key, e)
        except Exception as e:                   # ragged rows, bad parquet
            log.warning("embeddings unusable at %s: %s", self._embed_key, e)
        return [], None

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

    def _rows_of(self, sensitivities: Optional[Sequence[str]]) -> List[Dict]:
        rows = self.rows()
        if sensitivities is None:
            return rows
        return [r for r in rows if r.get("sensitivity") in sensitivities]

    def subjects(self, sensitivities: Optional[Sequence[str]] = None) -> List[str]:
        """Subject terms in use; with `sensitivities`, only on those rows."""
        seen = set()
        for r in self._rows_of(sensitivities):
            seen.update(r.get("subject") or [])
        return sorted(seen)

    def projects(self, sensitivities: Optional[Sequence[str]] = None) -> List[str]:
        return sorted({r.get("project") for r in self._rows_of(sensitivities)
                       if r.get("project")})

    # --- asking in plain words (finding-and-reuse.md §5) -------------------

    def has_vectors(self) -> bool:
        self._maybe_load()
        return self._vectors[1] is not None

    def rank(self, vector: Sequence[float],
             within: Optional[Set[str]] = None) -> List[Tuple[str, float]]:
        """Datasets with a vector, best match first, as (identifier,
        cosine). Empty when there are no vectors or the question's vector
        is from a different model (a different length)."""
        self._maybe_load()
        vec_ids, matrix = self._vectors
        if matrix is None:
            return []
        q = np.asarray(list(vector), dtype=np.float32)
        if q.ndim != 1 or q.shape[0] != matrix.shape[1]:
            return []
        n = float(np.linalg.norm(q)) or 1.0
        scores = matrix @ (q / n)
        out = []
        for i in np.argsort(-scores):
            ident = vec_ids[int(i)]
            if within is None or ident in within:
                out.append((ident, float(scores[int(i)])))
        return out

    def ask(self, question: str, platform, limit: Optional[int] = None,
            sensitivities: Sequence[str] = ("green",)) -> Answer:
        """A question in plain words to rows in order. Filter first (the
        model picks from the vocabulary; anything else is dropped), then
        rank by meaning where vectors exist, rows without a vector by the
        words, then the reranker over the top few. Each step that fails
        leaves a notice and the rest still runs; a step whose model name
        is blank is switched off and simply does not run. Abstracts of
        only the given sensitivities are ever sent to the reranker."""
        question = " ".join((question or "").split())[:500]
        ans = Answer(rows=[])
        if not question:
            return ans
        notices = []
        if getattr(platform, "chat_model", True):
            try:
                # Names found only on rows of other sensitivities stay out
                # of the prompt, like their abstracts.
                ans.filter = platform.filter_for(question, keys.STRANDS, keys.STATES,
                                                 keys.SENSITIVITIES,
                                                 self.subjects(sensitivities),
                                                 self.projects(sensitivities))
                ans.steps.append("filter")
            except PlatformError as e:
                notices.append("The question could not be interpreted (%s); "
                               "matching on its words instead." % e)
        f = ans.filter
        candidates = [r for r in self.search(strand=f.get("strand"), state=f.get("state"),
                                             sensitivity=f.get("sensitivity"),
                                             subject=f.get("subject"), project=f.get("project"))
                      if years_overlap(r, f.get("year_from"), f.get("year_to"))]
        if not candidates and f:
            # A filter too strict for what is in the store (with a small
            # store, easily): fall back to everything, ranked, and say so.
            candidates = self.rows()
            f = {k: v for k, v in f.items() if k == "words"}
            notices.append("Nothing matches all of that, so these are the closest "
                           "datasets instead.")
        words, all_of = (f["words"], True) if f.get("words") else (question, False)
        ranked: List[Dict] = []
        if self.has_vectors() and getattr(platform, "embed_model", True):
            try:
                vec = platform.embed([question])[0]
                by_id = {r["identifier"]: r for r in candidates}
                ranked = [by_id[i] for i, _ in self.rank(vec, set(by_id))]
                ans.steps.append("meaning")
            except PlatformError as e:
                notices.append("Ranking by meaning is unavailable (%s)." % e)
        seen = {r["identifier"] for r in ranked}
        rest = [r for r in candidates if r["identifier"] not in seen
                and words_match(r, words, all_of)]
        rows = ranked + rest
        if not rows and f and not f.get("words"):
            rows = candidates          # the filter alone was the answer
        if getattr(platform, "rerank_model", True):
            rows = self._rerank(question, platform, rows, set(sensitivities), ans, notices)
        named = [r for r in rows if name_match(r, question)]
        rows = named + [r for r in rows if r not in named]
        ans.rows = rows[:limit] if limit else rows
        ans.notice = " ".join(notices) or None
        return ans

    def _rerank(self, question, platform, rows, sensitivities, ans, notices) -> List[Dict]:
        top, tail = rows[:RERANK_TOP], rows[RERANK_TOP:]
        sendable = [i for i, r in enumerate(top) if r.get("sensitivity") in sensitivities]
        if len(sendable) < 2:
            return rows
        docs = [(top[i].get("dataset") or "") + ". " + (top[i].get("abstract") or "")
                for i in sendable]
        try:
            order = platform.rerank(question, docs)
        except PlatformError as e:
            notices.append("The reranker is unavailable (%s)." % e)
            return rows
        ans.steps.append("rerank")
        held = [top[i] for i in range(len(top)) if i not in sendable]
        return [top[sendable[j]] for j in order] + held + tail

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
