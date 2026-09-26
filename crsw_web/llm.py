"""A thin client for the KCL LLM platform (OpenAI-style endpoints).

Three calls, each one POST, no vendor SDK: embeddings, a chat completion
that turns a question into search filters, and a reranker. The promoter
uses `embed` when it builds the index; the web service uses all three
when a user asks the store a question (finding-and-reuse.md §5).

What is ever sent: record metadata (titles, abstracts, subject terms),
the vocabulary lists and the user's question. Never the contents of a
data file. Every failure is a `PlatformError` with a short message so
callers can fall back to the ordinary search; nothing here retries.

`from_settings` returns None when the platform is not configured, which
is the off setting: the service and the promoter run without it."""
import json
import logging
import re
from typing import Dict, List, Optional, Sequence

import httpx

log = logging.getLogger("crsw.llm")

DEFAULT_CHAT_MODEL = "arc:lite"
DEFAULT_EMBED_MODEL = "arc:embedvl"
DEFAULT_RERANK_MODEL = "arc:rerankvl"
DEFAULT_TIMEOUT = 20.0
DEFAULT_EMBED_DIMS = 1024   # keep the first N numbers of each vector (see embed)
EMBED_BATCH = 32

# The filter a question is turned into. Each value is checked against
# the allowed set before it is used; anything else is dropped.
FILTER_KEYS = ("strand", "state", "sensitivity", "subject", "project",
               "words", "year_from", "year_to")

FILTER_PROMPT = """You turn a researcher's question into search filters for a catalogue of datasets.
Reply with one JSON object and nothing else. Keys, all optional:
  "strand": one of {strands}
  "state": one of {states}
  "sensitivity": one of {sensitivities}
  "subject": one of the subject terms below
  "project": one of the project names below
  "words": a short phrase (at most six words) to match in titles and abstracts
  "year_from", "year_to": four-digit years the data should cover
Use a key only when the question clearly implies it. Do not invent values.
Subject terms: {subjects}
Project names: {projects}
Strands: rs1 origins and legacies of slavery, rs2 contemporary slavery and armed conflict, rs3 law and policy, rs4 culture and representation."""

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.S)
_YEAR = re.compile(r"^\d{4}$")


class PlatformError(RuntimeError):
    """The platform could not be used: network, HTTP status, or a reply
    that was not in the expected shape."""


class Platform:
    def __init__(self, base_url: str, api_key: str,
                 chat_model: str = DEFAULT_CHAT_MODEL,
                 embed_model: str = DEFAULT_EMBED_MODEL,
                 rerank_model: str = DEFAULT_RERANK_MODEL,
                 timeout: float = DEFAULT_TIMEOUT, embed_dims: Optional[int] = None,
                 transport=None):
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        # A blank model name switches that function off (the caller checks).
        self.chat_model = chat_model or ""
        self.embed_model = embed_model or ""
        self.rerank_model = rerank_model or ""
        self.embed_dims = embed_dims or None
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport,
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"})

    @classmethod
    def from_settings(cls, settings, transport=None) -> Optional["Platform"]:
        """None unless both the base URL and the key are set. Reads the
        `llm_*` fields the web Settings and the PromoterConfig share."""
        url = getattr(settings, "llm_base_url", "") or ""
        key = getattr(settings, "llm_api_key", "") or ""
        if not url or not key:
            return None
        return cls(url, key,
                   chat_model=getattr(settings, "llm_chat_model", DEFAULT_CHAT_MODEL),
                   embed_model=getattr(settings, "llm_embed_model", DEFAULT_EMBED_MODEL),
                   rerank_model=getattr(settings, "llm_rerank_model", DEFAULT_RERANK_MODEL),
                   timeout=getattr(settings, "llm_timeout_seconds", DEFAULT_TIMEOUT),
                   embed_dims=getattr(settings, "llm_embed_dims", None),
                   transport=transport)

    def close(self) -> None:
        self._client.close()

    # --- one POST -----------------------------------------------------------

    def _post(self, path: str, payload: Dict) -> Dict:
        try:
            r = self._client.post(path, json=payload)
        except httpx.HTTPError as e:
            raise PlatformError("platform unreachable: %s" % e.__class__.__name__)
        if r.status_code != 200:
            raise PlatformError("platform returned HTTP %d for %s" % (r.status_code, path))
        try:
            body = r.json()
        except ValueError:
            raise PlatformError("platform reply was not JSON for %s" % path)
        if not isinstance(body, dict):
            raise PlatformError("platform reply was not an object for %s" % path)
        return body

    # --- embeddings ---------------------------------------------------------

    def embed(self, texts: Sequence[str], batch: int = EMBED_BATCH) -> List[List[float]]:
        """One vector per text, in order. Sent in batches so a full
        rebuild of a large index is a few dozen requests, not thousands.
        With `embed_dims` set, each vector is cut to its first N numbers
        and scaled back to unit length: the Qwen embedding models are
        trained so a truncated vector still ranks well, and half the
        numbers is half the storage on the VM and in the bucket."""
        out: List[List[float]] = []
        texts = list(texts)
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            body = self._post("/embeddings", {"model": self.embed_model, "input": chunk})
            data = body.get("data")
            if not isinstance(data, list) or len(data) != len(chunk):
                raise PlatformError("embeddings reply had %s vectors for %d texts"
                                    % (len(data) if isinstance(data, list) else "no",
                                       len(chunk)))
            data = sorted(data, key=lambda d: d.get("index", 0))
            for d in data:
                vec = d.get("embedding")
                if not isinstance(vec, list) or not vec:
                    raise PlatformError("embeddings reply had an empty vector")
                out.append(self._cut([float(x) for x in vec]))
        return out

    def _cut(self, vec: List[float]) -> List[float]:
        if not self.embed_dims or len(vec) <= self.embed_dims:
            return vec
        vec = vec[:self.embed_dims]
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    # --- question to filter -------------------------------------------------

    def filter_for(self, question: str, strands: Sequence[str], states: Sequence[str],
                   sensitivities: Sequence[str], subjects: Sequence[str],
                   projects: Sequence[str]) -> Dict[str, str]:
        """The filters a question implies, each value checked against the
        allowed set. {} when the model answers with nothing usable: the
        caller then searches on the question's words alone."""
        prompt = FILTER_PROMPT.format(
            strands=", ".join(strands), states=", ".join(states),
            sensitivities=", ".join(sensitivities),
            subjects=", ".join(subjects) or "(none)",
            projects=", ".join(projects) or "(none)")
        body = self._post("/chat/completions", {
            "model": self.chat_model, "temperature": 0,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": question.strip()[:1000]}],
        })
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise PlatformError("chat reply had no message")
        raw = parse_json_object(text or "")
        if raw is None:
            log.info("question-to-filter reply was not JSON; searching on words only")
            return {}
        allowed = {"strand": set(strands), "state": set(states),
                   "sensitivity": set(sensitivities), "subject": set(subjects),
                   "project": set(projects)}
        out: Dict[str, str] = {}
        for k in FILTER_KEYS:
            v = raw.get(k)
            if v is None or v == "":
                continue
            if isinstance(v, list):          # a model that lists one subject
                v = v[0] if v else None
            if v is None:
                continue
            v = str(v).strip()
            if k in allowed:
                if v in allowed[k]:
                    out[k] = v
            elif k in ("year_from", "year_to"):
                if _YEAR.match(v):
                    out[k] = v
            elif k == "words":
                words = " ".join(v.split()[:6])
                if words:
                    out[k] = words
        return out

    # --- rerank -------------------------------------------------------------

    def rerank(self, question: str, documents: Sequence[str]) -> List[int]:
        """Indices of `documents`, best first, as the reranker orders
        them. Raises PlatformError like the rest; the caller keeps its
        own order when that happens."""
        docs = list(documents)
        if not docs:
            return []
        body = self._post("/rerank", {"model": self.rerank_model, "query": question,
                                      "documents": docs, "top_n": len(docs)})
        results = body.get("results")
        if not isinstance(results, list):
            raise PlatformError("rerank reply had no results")
        order = []
        for r in sorted(results, key=lambda r: -float(r.get("relevance_score", 0))):
            i = r.get("index")
            if isinstance(i, int) and 0 <= i < len(docs) and i not in order:
                order.append(i)
        order.extend(i for i in range(len(docs)) if i not in order)
        return order


def parse_json_object(text: str) -> Optional[Dict]:
    """The first JSON object in a model's reply, tolerating a thinking
    block, a code fence, or prose around it. None when there is none."""
    text = _THINK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
