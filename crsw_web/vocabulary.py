"""The vocabulary the running service uses, refreshed while it runs.

The service used to load the vocabulary once at start-up, so a change
merged in the vocabulary repository reached the form only on a restart.
This cache re-fetches at most once per `refresh_seconds`, inline on the
first request after the interval (one fetch, bounded by the fetch
timeout), and swaps the held copy only when the fetch produced a
remote or cached file that passes every authority rule. Any failure
keeps the copy already in hand, so the form never goes blank."""
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from crsw_deposit import authority, record, vocab


class VocabularyCache:
    def __init__(self, doc: Dict, source: str, refresh_seconds: int = 600,
                 loader: Optional[Callable[[], Tuple[Dict, str]]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.refresh_seconds = refresh_seconds
        self._loader = loader or vocab.load_vocabulary
        self._clock = clock
        self._lock = threading.Lock()
        self._set(doc, source)

    def _set(self, doc: Dict, source: str) -> None:
        self._doc = doc
        self._terms = vocab.all_terms(doc)
        self._codes = vocab.domain_codes(doc)
        self._source = source
        self._loaded_at = self._clock()
        self._loaded_iso = record.utc_now_iso()

    # --- reads --------------------------------------------------------------

    def current(self) -> Dict:
        self._maybe_refresh()
        return self._doc

    @property
    def terms(self) -> Set[str]:
        self._maybe_refresh()
        return self._terms

    @property
    def domain_codes(self) -> List[str]:
        self._maybe_refresh()
        return self._codes

    @property
    def source(self) -> str:
        return self._source

    @property
    def loaded(self) -> str:
        """When the held copy was loaded, UTC ISO."""
        return self._loaded_iso

    @property
    def version(self) -> Optional[str]:
        return self._doc.get("vocabulary_version")

    # --- refresh ------------------------------------------------------------

    def stale(self) -> bool:
        return (self.refresh_seconds > 0
                and self._clock() - self._loaded_at >= self.refresh_seconds)

    def _maybe_refresh(self) -> None:
        if not self.stale():
            return
        if not self._lock.acquire(blocking=False):
            return          # another request in this process is already on it
        try:
            if not self.stale():
                return
            self.refresh()
        finally:
            self._lock.release()

    def refresh(self) -> bool:
        """Try once; True if a newer usable file replaced the held copy.
        A bundled result means the fetch and the cache both failed, so
        the copy in hand (which is at least as good) stays. The clock is
        moved on either way, so a failing fetch is retried once per
        interval, not on every request."""
        try:
            doc, source = self._loader()
        except Exception:
            self._loaded_at = self._clock()
            return False
        usable = source in ("remote", "cache") and (
            not authority.is_authority(doc) or not authority.validate_authority(doc))
        if not usable or doc == self._doc:
            self._loaded_at = self._clock()
            return False
        self._set(doc, source)
        return True
