"""Subjects vocabulary: runtime fetch with cache and bundled fallback.

Chain (spec §6): HTTPS fetch (short timeout) -> local cache -> bundled
vocab.json. A slow or blocked network must never block a deposit.
No user I/O here — callers decide how to surface the 'source' value."""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import List, Optional, Set, Tuple

import keys

# Single line to update once the vocabulary repo is created.
VOCAB_URL = ("https://raw.githubusercontent.com/"
             "crsw-kcl/crsw-vocabulary/main/vocab.json")
FETCH_TIMEOUT = 4  # seconds


def bundled_vocab_path() -> Path:
    """Locate the bundled vocab.json, PyInstaller-compatible."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "vocab.json"


def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "crsw-deposit"
    return Path.home() / ".cache" / "crsw-deposit"


def _default_opener(url, timeout):
    return urllib.request.urlopen(url, timeout=timeout)


def _valid(vocab_dict) -> bool:
    return (isinstance(vocab_dict, dict)
            and isinstance(vocab_dict.get("facets"), dict)
            and all(isinstance(v, list) for v in vocab_dict["facets"].values()))


def _write_cache(cache: Path, raw: bytes) -> None:
    """Best-effort atomic cache write; failure to cache is never fatal."""
    try:
        cache.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cache), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, str(cache / "vocab.json"))
        except Exception:
            os.unlink(tmp)
            raise
    except OSError:
        pass


def load_vocabulary(url: str = VOCAB_URL,
                    timeout: int = FETCH_TIMEOUT,
                    cache: Optional[Path] = None,
                    bundled: Optional[Path] = None,
                    opener=None) -> Tuple[dict, str]:
    """Return (vocabulary, source) with source in remote/cache/bundled."""
    cache = cache if cache is not None else cache_dir()
    bundled = bundled if bundled is not None else bundled_vocab_path()
    opener = opener if opener is not None else _default_opener

    try:
        response = opener(url, timeout)
        raw = response.read()
        vocab_dict = json.loads(raw.decode("utf-8"))
        if _valid(vocab_dict):
            _write_cache(cache, raw)
            return vocab_dict, "remote"
    except Exception:
        pass

    cached_file = cache / "vocab.json"
    try:
        vocab_dict = json.loads(cached_file.read_text(encoding="utf-8"))
        if _valid(vocab_dict):
            return vocab_dict, "cache"
    except Exception:
        pass

    vocab_dict = json.loads(bundled.read_text(encoding="utf-8"))
    return vocab_dict, "bundled"


def all_terms(vocab_dict: dict) -> Set[str]:
    terms = set()
    for facet_terms in vocab_dict.get("facets", {}).values():
        terms.update(facet_terms)
    return terms


def domains(vocab_dict: dict) -> List[dict]:
    """Domain entries (code/label/steward). Falls back to the built-in
    codes so an old cached vocabulary never breaks deposits (r2 §2)."""
    entries = []
    for d in vocab_dict.get("domains") or []:
        if isinstance(d, dict) and d.get("code"):
            entries.append({"code": d["code"],
                            "label": d.get("label", ""),
                            "steward": d.get("steward", "")})
    if entries:
        return entries
    return [{"code": c, "label": "", "steward": ""} for c in keys.DOMAINS]


def domain_codes(vocab_dict: dict) -> List[str]:
    return [d["code"] for d in domains(vocab_dict)]
