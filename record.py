"""Dataset record: field definitions, validation, manifest logic, checksum.

NO user I/O in this module — the future web gateway imports it.
Schema reference: dataset record v0.4 (r5). One dataset.meta.json per
dataset prefix holds the descriptive fields and the files manifest;
per-file sidecars are retired (r5 §1 Q2)."""
import datetime
import getpass
import hashlib
import json
import mimetypes
import re
import uuid
from typing import List, Optional, Set, Tuple

import keys

SCHEMA_VERSION = "0.4"

REQUIRED_FIELDS = (
    "schema_version", "dataset_uuid", "identifier", "strand", "domain",
    "project", "state", "sensitivity", "coverage_start", "coverage_end",
    "version", "abstract", "subjects", "created", "modified", "files",
)
RECOMMENDED_FIELDS = (
    "vocabulary_version", "creator", "source_type", "source_detail",
    "licence", "steward", "depositors",
)
OPTIONAL_FIELDS = ("derived_from", "language", "ethics_ref", "spatial", "notes")

MANIFEST_REQUIRED = ("path", "checksum_sha256", "bytes")
MANIFEST_OPTIONAL = ("coverage_start", "coverage_end", "format",
                     "derived_from", "notes")

_VERSION_IN_RE = re.compile(r"^v?(\d+)[-.](\d+)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\d{4}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ABSTRACT_MIN_WORDS = 50


class RecordParseError(ValueError):
    """Raised when dataset.meta.json exists but cannot be used. Callers
    must surface this and stop — modifying a record that was never read
    would silently drop its manifest members (r5 §4)."""


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
    """Warn below 50 words; never block (r6 §0). Length guidance lives
    here in the tool, not in the schema - a contract constraint the tool
    doesn't enforce just breeds a permanently red CI test."""
    n = len((text or "").split())
    if n < ABSTRACT_MIN_WORDS:
        return ("Abstract is %d words. Guidance is at least %d - enough "
                "for someone deciding whether this dataset is worth "
                "requesting." % (n, ABSTRACT_MIN_WORDS))
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


def mint_uuid() -> str:
    """UUID4 for a dataset, minted once at first record creation (r5 Q1).
    Promotion or reclassification creates a NEW record with a new UUID;
    derived_from carries the lineage."""
    return str(uuid.uuid4())


# A private MimeTypes instance uses only the built-in table. The module
# default consults the Windows registry, which maps .csv to
# application/vnd.ms-excel - the manifest would then differ by the
# depositor's platform.
_MIME_TYPES = mimetypes.MimeTypes()


def guess_format(filename: str) -> Optional[str]:
    """MIME type from the filename, or None — omitted, never guessed (r5 §2)."""
    return _MIME_TYPES.guess_type(filename)[0]


# --- coverage envelope -------------------------------------------------
# Values are years ('1989') or ISO dates ('2025-12-31'), already checked
# by coverage_error. Comparison expands a year to its first/last day so
# ISO strings compare lexicographically; returned values keep whichever
# form the depositor gave.

def _as_start(value: str) -> str:
    return value + "-01-01" if _YEAR_RE.match(value or "") else (value or "")


def _as_end(value: str) -> str:
    return value + "-12-31" if _YEAR_RE.match(value or "") else (value or "")


def widen(envelope, pairs):
    """The smallest envelope containing `envelope` and every (start, end)
    pair in `pairs`. Pairs may contain None/empty ends, which are skipped."""
    start, end = envelope
    for pair_start, pair_end in pairs:
        if pair_start and _as_start(pair_start) < _as_start(start):
            start = pair_start
        if pair_end and _as_end(pair_end) > _as_end(end):
            end = pair_end
    return start, end


def envelope_errors(rec: dict) -> List[str]:
    """Containment check (r5 Q5): every per-file coverage range must sit
    inside the record's envelope."""
    errors = []
    start = rec.get("coverage_start")
    end = rec.get("coverage_end")
    if not start or not end:
        return errors
    for entry in rec.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("path") or "?"
        file_start = entry.get("coverage_start")
        file_end = entry.get("coverage_end")
        if file_start and _as_start(file_start) < _as_start(start):
            errors.append("file %r coverage starts %s, outside the dataset "
                          "envelope (%s to %s)" % (name, file_start, start, end))
        if file_end and _as_end(file_end) > _as_end(end):
            errors.append("file %r coverage ends %s, outside the dataset "
                          "envelope (%s to %s)" % (name, file_end, start, end))
    return errors


# --- manifest ----------------------------------------------------------

def manifest_entry(path_name: str, checksum_sha256: str, size_bytes: int,
                   coverage_start=None, coverage_end=None, fmt=None,
                   derived_from=None, notes=None) -> dict:
    """A files[] entry in spec §2 order; empty optionals are omitted."""
    entry = {
        "path": path_name,
        "checksum_sha256": checksum_sha256,
        "bytes": size_bytes,
    }
    extras = (
        ("coverage_start", coverage_start), ("coverage_end", coverage_end),
        ("format", fmt), ("derived_from", derived_from), ("notes", notes),
    )
    for name, value in extras:
        if value:
            entry[name] = value
    return entry


def merge_manifest(existing: Optional[List[dict]], new: List[dict]):
    """Union of an existing manifest and the current batch (r5 Q4).

    A new entry replaces the existing entry at the same path; existing
    paths are never dropped — removing a member is deliberate curation,
    out of scope for the tool. Returns (union sorted by path, added
    paths, changed paths), changed meaning same path, new checksum."""
    by_path = {entry["path"]: entry for entry in existing or []}
    added = []
    changed = []
    for entry in new:
        prior = by_path.get(entry["path"])
        if prior is None:
            added.append(entry["path"])
        elif prior.get("checksum_sha256") != entry.get("checksum_sha256"):
            changed.append(entry["path"])
        by_path[entry["path"]] = entry
    union = [by_path[path] for path in sorted(by_path)]
    return union, added, changed


def unchanged_paths(existing: Optional[List[dict]], new: List[dict]) -> Set[str]:
    """Paths whose upload can be skipped on a re-run: same path, same
    checksum, same size as the existing manifest entry."""
    by_path = {entry["path"]: entry for entry in existing or []}
    out = set()
    for entry in new:
        prior = by_path.get(entry["path"])
        if (prior is not None
                and prior.get("checksum_sha256") == entry.get("checksum_sha256")
                and prior.get("bytes") == entry.get("bytes")):
            out.add(entry["path"])
    return out


def append_depositor(depositors: Optional[List[str]], name: str) -> List[str]:
    """Depositors accumulate per deposit, first-seen order, no repeats."""
    out = list(depositors or [])
    if name and name not in out:
        out.append(name)
    return out


# --- record assembly and validation ------------------------------------

def build_record(dataset_uuid, identifier, strand, domain, project, state,
                 sensitivity, coverage_start, coverage_end, version, abstract,
                 subjects, files, created, modified, vocabulary_version=None,
                 creator=None, source_type=None, source_detail=None,
                 licence=None, steward=None, depositors=None,
                 derived_from=None, language=None, ethics_ref=None,
                 spatial=None, notes=None) -> dict:
    """Assemble the dataset record in spec §2 field order.
    Recommended/optional fields that are None or empty are omitted."""
    rec = {
        "schema_version": SCHEMA_VERSION,
        "dataset_uuid": dataset_uuid,
        "identifier": identifier,
        "strand": strand,
        "project": project,
        "sensitivity": sensitivity,
        "state": state,
        "domain": domain,
        "version": version,
        "abstract": abstract,
        "subjects": list(subjects),
    }
    if vocabulary_version:
        rec["vocabulary_version"] = vocabulary_version
    rec["coverage_start"] = coverage_start
    rec["coverage_end"] = coverage_end
    for name, value in (
            ("creator", creator), ("source_type", source_type),
            ("source_detail", source_detail), ("licence", licence),
            ("steward", steward), ("depositors", depositors)):
        if value:
            rec[name] = value
    rec["created"] = created
    rec["modified"] = modified
    for name, value in (
            ("derived_from", derived_from), ("language", language),
            ("ethics_ref", ethics_ref), ("spatial", spatial),
            ("notes", notes)):
        if value:
            rec[name] = value
    rec["files"] = list(files)
    return rec


def validate_record(rec: dict, vocab_terms: Set[str],
                    domain_codes: Optional[List[str]] = None
                    ) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block a deposit; warnings do not.
    Hand-rolled on purpose: the shipped tool cannot use jsonschema (stdlib
    constraint) — dataset.schema.json is the contract artefact for CI and
    the gateway, this function is the runtime check (r5 §7).
    domain_codes: valid codes from the fetched vocabulary; None falls back
    to the built-in list (r2 §2 - domains are fetched, not hardcoded)."""
    errors = []
    warnings = []
    for field in REQUIRED_FIELDS:
        if field not in rec or rec[field] in (None, "", []):
            errors.append("required field '%s' is missing or empty" % field)
    if errors:
        return errors, warnings

    if rec["strand"] not in keys.STRANDS:
        errors.append("strand %r is not one of %s"
                      % (rec["strand"], "/".join(keys.STRANDS)))
    valid_domains = list(domain_codes) if domain_codes else list(keys.DOMAINS)
    if rec["domain"] not in valid_domains:
        errors.append("domain %r is not one of %s"
                      % (rec["domain"], "/".join(valid_domains)))
    if rec["state"] not in keys.STATES:
        errors.append("state %r is not one of %s"
                      % (rec["state"], "/".join(keys.STATES)))
    if rec["sensitivity"] not in keys.SENSITIVITIES:
        errors.append("sensitivity %r is not green or amber" % rec["sensitivity"])

    expected_id = "/".join((rec["strand"], rec["project"],
                            rec["sensitivity"], rec["state"]))
    if rec["identifier"] != expected_id:
        errors.append("identifier %r does not match the path parts (%r)"
                      % (rec["identifier"], expected_id))
    if not _UUID4_RE.match(str(rec["dataset_uuid"])):
        errors.append("dataset_uuid %r is not a UUID4" % (rec["dataset_uuid"],))

    for field in ("coverage_start", "coverage_end"):
        err = coverage_error(rec[field])
        if err:
            errors.append(err)

    if not isinstance(rec["subjects"], list) or not rec["subjects"]:
        errors.append("at least one subject term is required")
    else:
        for term in unknown_subjects(rec["subjects"], vocab_terms):
            errors.append(
                "subject %r is not in the vocabulary; to propose an addition, "
                "open a pull request or issue on the vocabulary repo" % term)

    warn = abstract_warning(rec["abstract"])
    if warn:
        warnings.append(warn)
    canonical = normalise_version(rec["version"])
    if canonical is None:
        errors.append("version %r is not two integers like 3-0" % (rec["version"],))
    elif canonical != rec["version"]:
        warnings.append("version %r should be stored as %r"
                        % (rec["version"], canonical))

    for field in ("depositors", "language"):
        if field in rec and not isinstance(rec[field], list):
            errors.append("'%s' must be a list" % field)

    if not isinstance(rec["files"], list):
        errors.append("'files' must be a list of manifest entries")
        return errors, warnings
    seen_paths = set()
    for i, entry in enumerate(rec["files"]):
        if not isinstance(entry, dict):
            errors.append("files entry %d is not an object" % (i + 1))
            continue
        name = entry.get("path") or ("entry %d" % (i + 1))
        for field in MANIFEST_REQUIRED:
            if field not in entry or entry[field] in (None, ""):
                errors.append("file %s: required field '%s' is missing or "
                              "empty" % (name, field))
        path = entry.get("path")
        if path == keys.RECORD_FILENAME:
            errors.append("a manifest entry uses the reserved name %r"
                          % keys.RECORD_FILENAME)
        if path:
            if path in seen_paths:
                errors.append("duplicate manifest path %r" % path)
            seen_paths.add(path)
        checksum = entry.get("checksum_sha256")
        if checksum and not _SHA256_RE.match(str(checksum)):
            errors.append("file %s: checksum_sha256 is not a 64-character "
                          "SHA-256 hex digest" % name)
        size = entry.get("bytes")
        if size is not None and size != "" and (
                not isinstance(size, int) or isinstance(size, bool) or size < 0):
            errors.append("file %s: bytes must be a non-negative integer" % name)
        for field in ("coverage_start", "coverage_end"):
            if entry.get(field):
                err = coverage_error(entry[field])
                if err:
                    errors.append("file %s: %s" % (name, err))
    errors.extend(envelope_errors(rec))
    return errors, warnings


def record_json(rec: dict) -> str:
    return json.dumps(rec, indent=2, ensure_ascii=False) + "\n"


def parse_record(text: str) -> dict:
    """Parse an existing dataset.meta.json fetched from storage.
    Raises RecordParseError for anything the tool must not build on:
    invalid JSON, a non-object, a schema_version it does not understand
    (including newer ones — never blind-overwrite the future), or a
    missing/empty files manifest. Absence-vs-fetch-error is the transfer
    layer's distinction, not this function's."""
    try:
        rec = json.loads(text)
    except ValueError as e:
        raise RecordParseError("not valid JSON: %s" % e)
    if not isinstance(rec, dict):
        raise RecordParseError("not a JSON object")
    if rec.get("schema_version") != SCHEMA_VERSION:
        raise RecordParseError(
            "schema_version %r is not %r; refusing to modify a record this "
            "version of the tool does not understand"
            % (rec.get("schema_version"), SCHEMA_VERSION))
    files = rec.get("files")
    if not isinstance(files, list) or not files:
        raise RecordParseError("the files manifest is missing or empty")
    return rec
