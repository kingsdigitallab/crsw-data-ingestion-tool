"""A thin client for the KCL LLM platform (OpenAI-style endpoints).

Each call one POST, no vendor SDK: embeddings, a reranker, and the chat
model for one sentence on why a result matched (and, in the promoter,
for reading a scanned page). The promoter uses `embed` when it builds
the index and the passages; the web service uses all of them on the
search page (finding-and-reuse.md §5 and §7). The chat model drove a
question-to-filter step on the find page until 22 October 2026, when
that step was retired (docs/ask-and-search-handoff.md).

What is ever sent: record metadata (titles, abstracts), passages of
files whose sensitivity allows it, and the user's words. Every failure
is a `PlatformError` with a short message so callers can fall back to
the ordinary search; nothing here retries.

`from_settings` returns None when the platform is not configured, which
is the off setting: the service and the promoter run without it."""
import base64
import json
import logging
import math
import re
import time
from typing import Dict, List, Optional, Sequence

import httpx

log = logging.getLogger("crsw.llm")

DEFAULT_CHAT_MODEL = "arc:lite"
DEFAULT_EMBED_MODEL = "arc:embedvl"
DEFAULT_RERANK_MODEL = "arc:rerankvl"
DEFAULT_TIMEOUT = 20.0
DEFAULT_EMBED_DIMS = 1024   # keep the first N numbers of each vector (see embed)
EMBED_BATCH = 32
# Qwen3-Embedding is trained asymmetrically: a query carries an
# instruction, a document does not. This is the model's general-purpose
# retrieval instruction, put on the question only (never on anything
# stored), in the documented "Instruct: ...\nQuery: ..." form. The web
# Settings carry it as `llm_query_instruction`; blank switches it off.
QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
# A 429 is the platform asking for a pause, not a failure: wait what
# Retry-After says, else these seconds, and ask again; after the last
# pause the next refusal is the PlatformError. A passages rebuild of a
# long PDF is hundreds of batches and met the limit on 19 October 2026;
# a query on the web page is one request and rarely does. A 503 (the
# platform down) is not retried: a page must not stall for fifteen
# seconds, and the callers already degrade.
RETRY_SLEEPS = (1.0, 2.0, 4.0, 8.0)
RETRY_STATUSES = (429,)
MAX_RETRY_AFTER = 60.0
# How long a resolved embed fingerprint is trusted before GET /models is
# asked again (the web passes its index cycle; the promoter asks once).
DEFAULT_FINGERPRINT_SECONDS = 300.0

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.S)


class PlatformError(RuntimeError):
    """The platform could not be used: network, HTTP status, or a reply
    that was not in the expected shape."""


class Platform:
    def __init__(self, base_url: str, api_key: str,
                 chat_model: str = DEFAULT_CHAT_MODEL,
                 embed_model: str = DEFAULT_EMBED_MODEL,
                 rerank_model: str = DEFAULT_RERANK_MODEL,
                 timeout: float = DEFAULT_TIMEOUT, embed_dims: Optional[int] = None,
                 fingerprint_seconds: float = DEFAULT_FINGERPRINT_SECONDS,
                 ocr_model: str = "", transport=None):
        if not base_url or not api_key:
            raise ValueError("base_url and api_key are required")
        self.base_url = base_url.rstrip("/")
        # A blank model name switches that function off (the caller checks).
        self.chat_model = chat_model or ""
        self.embed_model = embed_model or ""
        self.rerank_model = rerank_model or ""
        self.ocr_model = ocr_model or ""      # off unless named: a vision-capable chat alias
        self.embed_dims = embed_dims or None
        self.fingerprint_seconds = fingerprint_seconds
        self._fingerprint: Optional[str] = None      # last good answer
        self._fingerprint_at: Optional[float] = None
        self._fingerprint_said: Optional[str] = None  # last warning, so it is said once
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
                   fingerprint_seconds=getattr(settings, "index_refresh_seconds",
                                               DEFAULT_FINGERPRINT_SECONDS),
                   ocr_model=getattr(settings, "llm_ocr_model", ""),
                   transport=transport)

    def close(self) -> None:
        self._client.close()

    # --- one request --------------------------------------------------------

    def _post(self, path: str, payload: Dict) -> Dict:
        return self._send(lambda: self._client.post(path, json=payload), path)

    def _get(self, path: str) -> Dict:
        return self._send(lambda: self._client.get(path), path)

    def _send(self, call, path: str) -> Dict:
        """One request, repeated after a pause when the platform says
        it is busy (RETRY_STATUSES); any other failure is immediate."""
        for pause in RETRY_SLEEPS + (None,):
            try:
                r = call()
            except httpx.HTTPError as e:
                raise PlatformError("platform unreachable: %s" % e.__class__.__name__)
            if r.status_code not in RETRY_STATUSES or pause is None:
                return self._body(r, path)
            asked = r.headers.get("Retry-After")
            try:
                wait = min(float(asked), MAX_RETRY_AFTER) if asked else pause
            except ValueError:
                wait = pause
            log.info("platform %d for %s; waiting %.0fs", r.status_code, path, wait)
            time.sleep(wait)
        raise PlatformError("unreachable")        # the loop returns or raises

    def _body(self, r, path: str) -> Dict:
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

    def embed(self, texts: Sequence[str], batch: int = EMBED_BATCH,
              instruction: Optional[str] = None) -> List[List[float]]:
        """One vector per text, in order. Sent in batches so a full
        rebuild of a large index is a few dozen requests, not thousands.
        With `embed_dims` set, each vector is cut to its first N numbers
        and scaled back to unit length: the Qwen embedding models are
        trained so a truncated vector still ranks well, and half the
        numbers is half the storage on the VM and in the bucket.
        With `instruction` set, each text is sent as a *query* in the
        model's instructed form (see QUERY_INSTRUCTION); documents, the
        texts a query is matched against, are sent bare, which is the
        asymmetry the model is trained on. None or blank sends texts as
        they are."""
        out: List[List[float]] = []
        texts = list(texts)
        if instruction:
            texts = ["Instruct: %s\nQuery: %s" % (instruction, t) for t in texts]
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            body = self._post("/embeddings", {"model": self.embed_model, "input": chunk})
            data = body.get("data")
            if not isinstance(data, list) or len(data) != len(chunk):
                raise PlatformError("embeddings reply had %s vectors for %d texts"
                                    % (len(data) if isinstance(data, list) else "no",
                                       len(chunk)))
            if not all(isinstance(d, dict) for d in data):
                raise PlatformError("embeddings reply was not a list of objects")
            data = sorted(data, key=lambda d: _number(d.get("index", 0), "embedding index"))
            for d in data:
                vec = d.get("embedding")
                if not isinstance(vec, list) or not vec:
                    raise PlatformError("embeddings reply had an empty vector")
                out.append(self._cut([_number(x, "embedding") for x in vec]))
        return out

    def _cut(self, vec: List[float]) -> List[float]:
        if not self.embed_dims or len(vec) <= self.embed_dims:
            return vec
        vec = vec[:self.embed_dims]
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]

    # --- what the embed alias is today ---------------------------------------

    def embed_fingerprint(self) -> str:
        """What the embed alias currently resolves to, as one string:
        `alias|backend_model|backend_model_quant|created` from the
        platform's model listing (LiteLLM's custom fields; `created`
        changes when the alias is re-registered with the same backend).
        Vectors are keyed to this, so a backend swapped behind an
        unchanged alias still re-embeds. Never raises: without the
        listing or its fields the alias alone is the fingerprint, and a
        listing that fails after a good answer keeps that answer. Cached
        for `fingerprint_seconds`."""
        alias = self.embed_model
        if not alias:
            return ""
        now = time.monotonic()
        if (self._fingerprint is not None and self._fingerprint_at is not None
                and now - self._fingerprint_at < self.fingerprint_seconds):
            return self._fingerprint
        try:
            body = self._get("/models")
        except PlatformError as e:
            if self._fingerprint is not None:
                self._say("model listing failed (%s); keeping %s" % (e, self._fingerprint))
                return self._fingerprint
            self._say("model listing failed (%s); vectors keyed to the alias %s only"
                      % (e, alias))
            return alias
        listed = body.get("data")
        entry = next((m for m in (listed if isinstance(listed, list) else [])
                      if isinstance(m, dict) and m.get("id") == alias), None)
        if entry and entry.get("backend_model"):
            value = "|".join(str(entry.get(k) or "") for k in
                             ("id", "backend_model", "backend_model_quant", "created"))
        else:
            self._say("model listing has no backend fields for %s; vectors keyed to "
                      "the alias only" % alias)
            value = alias
        self._fingerprint, self._fingerprint_at = value, now
        return value

    def _say(self, message: str) -> None:
        if message != self._fingerprint_said:
            log.warning(message)
            self._fingerprint_said = message

    # --- one sentence on why a passage matched ------------------------------

    EXPLAIN_PROMPT = ("A researcher searched a document store. Say in one plain sentence "
                      "what in the passage relates to what they asked, or say plainly that "
                      "nothing in it does. Quote nothing longer than a few words. No preamble.")

    def explain(self, question: str, passage: str) -> str:
        """The chat model's one-sentence reading of what connects a
        passage to a question. A reading after the fact, not the reason
        the passage was found; callers say so."""
        # No thinking pass: it is a one-sentence reading, and with thinking
        # on the budget went on the thinking and the answer came back empty.
        body = self._post("/chat/completions", {
            "model": self.chat_model, "temperature": 0, "max_tokens": 200,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": self.EXPLAIN_PROMPT},
                         {"role": "user", "content": "Asked: %s\n\nPassage:\n%s"
                                                     % (question.strip()[:500], passage.strip()[:4000])}],
        })
        text = _THINK.sub("", _chat_text(body)).strip()
        return " ".join(text.split())[:400]

    # --- a page image read as text -------------------------------------------

    TRANSCRIBE_PROMPT = ("Transcribe every word printed on this page exactly as it appears, "
                         "in reading order, as plain text. Do not summarise, correct, "
                         "translate or add anything. If the page has no readable text, "
                         "answer with the single word NONE.")

    def transcribe(self, image: bytes, mime: str = "image/jpeg") -> str:
        """The text printed on one page image, read by the vision-capable
        chat alias `ocr_model` (promoter/passages.py sends the first pages
        of a PDF that has no text layer). "" when the page has none.
        Raises PlatformError like the rest; the caller keeps the file's
        no-text marker when that happens."""
        url = "data:%s;base64,%s" % (mime, base64.b64encode(image).decode("ascii"))
        body = self._post("/chat/completions", {
            "model": self.ocr_model, "temperature": 0, "max_tokens": 3000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": self.TRANSCRIBE_PROMPT},
                         {"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": url}}]}],
        })
        text = _THINK.sub("", _chat_text(body)).strip()
        return "" if text.upper() == "NONE" else text

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
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise PlatformError("rerank reply had no results")
        order = []
        for r in sorted(results, key=lambda r: -_number(r.get("relevance_score", 0), "relevance score")):
            i = r.get("index")
            if type(i) is int and 0 <= i < len(docs) and i not in order:
                order.append(i)
        order.extend(i for i in range(len(docs)) if i not in order)
        return order


def _number(x, what: str) -> float:
    """A finite number; true/false, NaN and infinities are refused."""
    try:
        if isinstance(x, bool):
            raise ValueError(x)
        n = float(x)
        if not math.isfinite(n):
            raise ValueError(x)
        return n
    except (TypeError, ValueError, OverflowError):
        raise PlatformError("platform reply had a %s that is not a number" % what)


def _chat_text(body: Dict) -> str:
    """The first choice's message text; a missing or non-text message is
    a PlatformError."""
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise PlatformError("chat reply had no message")
    if text is not None and not isinstance(text, str):
        raise PlatformError("chat reply's message was not text")
    return text or ""


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
