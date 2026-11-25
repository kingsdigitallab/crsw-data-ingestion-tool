"""Places: the UN M49 geoscheme as a small hierarchy.

`places.json` lists the world, its regions, subregions and
intermediate regions (`m49:NNN`) and every country or area (`iso:XX`,
ISO 3166 alpha-2; `m49:NNN` for the few the scheme gives no alpha-2
code), each with a label, a kind and a `broader` parent. The file is
generated in the vocabulary repo and never edited by hand. It is loaded
by the same chain as the vocabulary: fetch, then cache, then the
bundled copy. A record's `spatial` is a list of these codes.

Pure functions, stdlib only, no user I/O."""
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import record
from . import vocab

PLACES_URL = ("https://raw.githubusercontent.com/"
              "kingsdigitallab/crsw-vocabulary/main/places.json")
CODE_RE = record.PLACE_CODE_RE
KINDS = ("world", "region", "subregion", "intermediate", "country")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What expand_place reports.
EXPANDED_EXACT = "exact"          # the code alone: nothing above or below it
EXPANDED_HIERARCHY = "hierarchy"  # the code with what contains it and what it contains
EXPANDED_UNKNOWN = "unknown"      # not a place in the file: matched as typed


def bundled_places_path() -> Path:
    """Return the path of the bundled places.json, in a checkout or inside
    a PyInstaller build."""
    # A PyInstaller build adds the file beside vocab.json:
    # datas=[("crsw_deposit/places.json", "crsw_deposit")].
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "crsw_deposit" / "places.json"
    return Path(__file__).resolve().parent / "places.json"


# --- validation --------------------------------------------------------

def validate_places(doc) -> List[str]:
    """Return every rule the file breaks, as plain messages. Empty means
    the file is good."""
    errors = []
    if not isinstance(doc, dict):
        return ["places is not a JSON object"]
    version = doc.get("places_version")
    if not isinstance(version, str) or not DATE_RE.match(version):
        errors.append("places_version must be a date like 2026-10-21")
    places = doc.get("places")
    if not isinstance(places, list) or not places:
        return errors + ["places must be a non-empty list"]
    by_code = {}
    for i, p in enumerate(places):
        label = "places[%d]" % (i + 1)
        if not isinstance(p, dict):
            errors.append("%s is not an object" % label)
            continue
        code = p.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            errors.append("%s: code %r is not m49:NNN or iso:XX" % (label, code))
            continue
        label = "place %s" % code
        if code in by_code:
            errors.append("%s appears twice" % label)
            continue
        by_code[code] = p
        if not isinstance(p.get("label"), str) or not p["label"].strip():
            errors.append("%s: label is missing" % label)
        if p.get("kind") not in KINDS:
            errors.append("%s: kind must be one of %s" % (label, "/".join(KINDS)))
        broader = p.get("broader")
        if broader is not None and not isinstance(broader, str):
            errors.append("%s: broader must be a code or null" % label)
    for code, p in by_code.items():
        broader = p.get("broader")
        if isinstance(broader, str):
            if broader not in by_code:
                errors.append("place %s: broader names unknown place %r" % (code, broader))
            elif code in _ancestors(by_code, code):
                errors.append("place %s: broader chain loops" % code)
    return errors


def _valid(doc) -> bool:
    return not validate_places(doc)


# --- loading -----------------------------------------------------------

def load_places(url: str = PLACES_URL, timeout: int = vocab.FETCH_TIMEOUT,
                cache: Optional[Path] = None, bundled: Optional[Path] = None,
                opener=None) -> Tuple[dict, str]:
    """Return (places, source), with source one of remote, cache or
    bundled. A fetched or cached file is used only if it passes
    validate_places; the bundled copy is the last resort."""
    cache = cache if cache is not None else vocab.cache_dir()
    bundled = bundled if bundled is not None else bundled_places_path()
    opener = opener if opener is not None else vocab._default_opener
    try:
        raw = opener(url, timeout).read()
        doc = json.loads(raw.decode("utf-8"))
        if _valid(doc):
            vocab._write_cache(cache, raw, "places.json")
            return doc, "remote"
    except Exception:
        pass
    try:
        doc = json.loads((cache / "places.json").read_text(encoding="utf-8"))
        if _valid(doc):
            return doc, "cache"
    except Exception:
        pass
    return json.loads(bundled.read_text(encoding="utf-8")), "bundled"


# --- reading -----------------------------------------------------------

def places_by_code(doc: Optional[dict]) -> Dict[str, dict]:
    return {p["code"]: p for p in (doc or {}).get("places") or []
            if isinstance(p, dict) and isinstance(p.get("code"), str)}


def codes(doc: Optional[dict]) -> Set[str]:
    return set(places_by_code(doc))


def labels_of(doc: Optional[dict]) -> Dict[str, str]:
    return {c: p.get("label") or c for c, p in places_by_code(doc).items()}


def children_of(doc: dict, code: str) -> List[str]:
    return [c for c, p in places_by_code(doc).items() if p.get("broader") == code]


def narrower(doc: dict, code: str) -> List[str]:
    """Return every place under `code`, in tree order, without the code
    itself."""
    out = []
    for child in children_of(doc, code):
        out.append(child)
        out.extend(narrower(doc, child))
    return out


def _ancestors(by_code: Dict[str, dict], code: str) -> List[str]:
    out = []
    seen = set()
    parent = (by_code.get(code) or {}).get("broader")
    while isinstance(parent, str) and parent not in seen:
        out.append(parent)
        seen.add(parent)
        parent = (by_code.get(parent) or {}).get("broader")
    return out


def ancestors(doc: dict, code: str) -> List[str]:
    """Return what contains `code`, nearest first."""
    return _ancestors(places_by_code(doc), code)


def tree(doc: dict) -> List[Tuple[str, int]]:
    """Return every place as (code, depth), roots first, children after
    their parent in file order."""
    by_code = places_by_code(doc)
    out = []

    def walk(code, depth):
        out.append((code, depth))
        for child in children_of(doc, code):
            walk(child, depth + 1)

    for code, p in by_code.items():
        if p.get("broader") not in by_code:
            walk(code, 0)
    return out


def expand_place(doc: Optional[dict], code: str) -> Tuple[List[str], str]:
    """Return (codes, how) for a search on `code`: the places that contain
    it, the code itself and the places it contains. `how` is one of the
    EXPANDED_ constants. An unknown code, or no places document at all,
    gives the code alone."""
    by_code = places_by_code(doc)
    if code not in by_code:
        return [code], EXPANDED_UNKNOWN
    above = _ancestors(by_code, code)
    below = narrower(doc, code)
    if not above and not below:
        return [code], EXPANDED_EXACT
    return above[::-1] + [code] + below, EXPANDED_HIERARCHY
