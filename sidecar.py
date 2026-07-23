"""Sidecar metadata: field definitions, validation, assembly, checksum.

NO user I/O in this module — the future web gateway imports it.
Schema reference: Sidecar Metadata Specification v0.2 (spec §5)."""
import datetime
import getpass
import hashlib
import json
import re
from typing import List, Optional, Set, Tuple

import keys

SCHEMA_VERSION = "0.3"

REQUIRED_FIELDS = (
    "schema_version", "object_key", "strand", "domain", "project",
    "state", "sensitivity", "coverage_start", "coverage_end",
    "version", "abstract", "subjects",
)
RECOMMENDED_FIELDS = (
    "vocabulary_version", "source_type", "source_detail", "depositor",
    "deposited", "checksum_sha256", "licence", "steward",
)
OPTIONAL_FIELDS = ("derived_from", "language", "ethics_ref", "notes")

_VERSION_IN_RE = re.compile(r"^v?(\d+)[-.](\d+)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\d{4}$")

ABSTRACT_MIN_WORDS = 100
ABSTRACT_MAX_WORDS = 300


def sha256_file(path, chunk_size=1024 * 1024) -> str:
    """Chunked SHA-256 so a 10GB raster never loads into memory."""
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def coverage_error(value: str) -> Optional[str]:
    """Valid: a 4-digit year ('1989') or an ISO date ('2025-12-31')."""
    if _YEAR_RE.match(value or ""):
        return None
    try:
        datetime.date.fromisoformat(value)
        return None
    except (ValueError, TypeError):
        return ("%r is not a valid coverage value; use a year like 1989 "
                "or an ISO date like 2025-12-31" % (value,))


def abstract_warning(text: str) -> Optional[str]:
    """Warn outside 100-300 words; never block (spec §5)."""
    n = len((text or "").split())
    if n < ABSTRACT_MIN_WORDS or n > ABSTRACT_MAX_WORDS:
        return "abstract is %d words; aim for %d-%d" % (
            n, ABSTRACT_MIN_WORDS, ABSTRACT_MAX_WORDS)
    return None


def normalise_version(value: str) -> Optional[str]:
    """Normalise version input to '{major}-{minor}' (r2 §3).
    Accepts 3-0 / v3-0 / 3.0 / v3.0; returns None for anything else -
    a bare '3' is rejected, never guessed."""
    m = _VERSION_IN_RE.match((value or "").strip())
    if not m:
        return None
    return "%s-%s" % (m.group(1), m.group(2))


def unknown_subjects(subjects: List[str], vocab_terms: Set[str]) -> List[str]:
    return [s for s in subjects if s not in vocab_terms]


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_depositor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def build_sidecar(object_key, strand, domain, project, state, sensitivity,
                  coverage_start, coverage_end, version, abstract, subjects,
                  vocabulary_version=None, source_type=None,
                  source_detail=None, depositor=None,
                  deposited=None, checksum_sha256=None, licence=None,
                  steward=None, derived_from=None, language=None,
                  ethics_ref=None, notes=None) -> dict:
    """Assemble the sidecar dict in spec §5 field order.
    Recommended/optional fields that are None or empty are omitted."""
    sc = {
        "schema_version": SCHEMA_VERSION,
        "object_key": object_key,
        "strand": strand,
        "domain": domain,
        "project": project,
        "state": state,
        "sensitivity": sensitivity,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "version": version,
        "abstract": abstract,
        "subjects": list(subjects),
    }
    extras = (
        ("vocabulary_version", vocabulary_version),
        ("source_type", source_type), ("source_detail", source_detail),
        ("depositor", depositor), ("deposited", deposited),
        ("checksum_sha256", checksum_sha256), ("licence", licence),
        ("steward", steward), ("derived_from", derived_from),
        ("language", language), ("ethics_ref", ethics_ref), ("notes", notes),
    )
    for name, value in extras:
        if value:
            sc[name] = value
    return sc


def validate_sidecar(sc: dict, vocab_terms: Set[str],
                     domain_codes: Optional[List[str]] = None
                     ) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block a deposit; warnings do not.
    domain_codes: valid codes from the fetched vocabulary; None falls back
    to the built-in list (r2 §2 - domains are fetched, not hardcoded)."""
    errors = []
    warnings = []
    for field in REQUIRED_FIELDS:
        if field not in sc or sc[field] in (None, "", []):
            errors.append("required field '%s' is missing or empty" % field)
    if errors:
        return errors, warnings

    if sc["strand"] not in keys.STRANDS:
        errors.append("strand %r is not one of %s" % (sc["strand"], "/".join(keys.STRANDS)))
    valid_domains = list(domain_codes) if domain_codes else list(keys.DOMAINS)
    if sc["domain"] not in valid_domains:
        errors.append("domain %r is not one of %s"
                      % (sc["domain"], "/".join(valid_domains)))
    if sc["state"] not in keys.STATES:
        errors.append("state %r is not one of %s" % (sc["state"], "/".join(keys.STATES)))
    if sc["sensitivity"] not in keys.SENSITIVITIES:
        errors.append("sensitivity %r is not green or amber" % sc["sensitivity"])

    for field in ("coverage_start", "coverage_end"):
        err = coverage_error(sc[field])
        if err:
            errors.append(err)

    if not isinstance(sc["subjects"], list) or not sc["subjects"]:
        errors.append("at least one subject term is required")
    else:
        for term in unknown_subjects(sc["subjects"], vocab_terms):
            errors.append(
                "subject %r is not in the vocabulary; to propose an addition, "
                "open a pull request or issue on the vocabulary repo" % term)

    warn = abstract_warning(sc["abstract"])
    if warn:
        warnings.append(warn)
    canonical = normalise_version(sc["version"])
    if canonical is None:
        errors.append("version %r is not two integers like 3-0" % (sc["version"],))
    elif canonical != sc["version"]:
        warnings.append("version %r should be stored as %r"
                        % (sc["version"], canonical))
    return errors, warnings


def sidecar_json(sc: dict) -> str:
    return json.dumps(sc, indent=2, ensure_ascii=False) + "\n"
