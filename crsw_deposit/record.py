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
from urllib.parse import urlsplit

from . import authority
from . import keys

# Every tool writes SCHEMA_VERSION. parse_record also reads the older
# versions in ACCEPTED_SCHEMA_VERSIONS and upgrades them on the way in
# (r8 §5): nobody converts a record by hand. 0.7 (r11 §2): `domain` is
# a list, primary first; `provider` (slugs) and `spatial_note` are new;
# `spatial` is a list of place codes.
SCHEMA_VERSION = "0.7"
ACCEPTED_SCHEMA_VERSIONS = ("0.5", "0.6", "0.7")

REQUIRED_FIELDS = (
    "schema_version", "dataset_uuid", "identifier", "strand", "domain",
    "project", "dataset", "state", "sensitivity", "temporal",
    "version", "abstract", "subject", "created", "modified", "files",
)
RECOMMENDED_FIELDS = (
    "vocabulary_version", "creator", "source_type", "source_detail",
    "provider", "license", "steward", "depositors",
)
OPTIONAL_FIELDS = ("derived_from", "provenance", "category_history",
                   "language", "ethics_ref", "spatial", "spatial_note", "notes")

# r11 §4: a place code in `spatial`: a UN M49 node or an ISO 3166
# alpha-2 country. The hierarchy itself is places.json (crsw_deposit.places).
PLACE_CODE_RE = re.compile(r"^(m49:[0-9]{3}|iso:[A-Z]{2})$")

MANIFEST_REQUIRED = ("path", "checksum_sha256", "bytes")
MANIFEST_OPTIONAL = ("temporal", "format", "derived_from", "notes")

# r8 §1: a derived_from reference is a dataset in the store or something
# outside it. r8 §2: an activity is one transformation step; the kinds
# are vocabulary-managed (vocab.activity_codes) with this fallback.
REFERENCE_KINDS = ("dataset", "external")
ACTIVITY_KINDS = ("convert", "clean", "harmonise", "aggregate", "geocode",
                  "anonymise", "merge", "subset", "manual", "other")
_URL_SCHEMES = ("http", "https")

# r9: what a category_history entry can say happened to a record's
# subject terms or domain after deposit. `by` is a username, or
# "vocabulary" when a vocabulary change was applied mechanically.
# r11: `field_set`, one entry per descriptive field a steward set in
# place; the entry names the field.
HISTORY_KINDS = ("added", "removed", "replaced", "split_review",
                 "domain_changed", "field_set")
VOCABULARY_ACTOR = "vocabulary"

# The source_type enum: the schema, the CLI menu and the web form all
# read it from here.
SOURCE_TYPES = ("archive", "survey", "scrape", "instrument", "partner",
                "derived", "other")

# r11: the fields a change file may set in place, in record order, each
# with its shape. Only descriptive recommended/optional fields: `subject`
# and `domain` have their own change-file keys (add/remove/set_domain)
# and their own history kinds; `derived_from`, `provenance`, `depositors`
# and `category_history` accumulate through deposit and are never
# overwritten; every required field is identity or the manifest and
# changes only through a new deposit.
SETTABLE_FIELDS = (
    ("creator", "text"), ("source_type", "source_type"),
    ("source_detail", "text"), ("provider", "provider"), ("license", "text"),
    ("steward", "text"), ("language", "list"), ("ethics_ref", "text"),
    ("spatial", "place"), ("spatial_note", "text"), ("notes", "text"),
)

_VERSION_IN_RE = re.compile(r"^v?(\d+)[-.](\d+)$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^\d{4}$")
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile("^(%s)/[a-z0-9-]+/(%s)/(%s)/[a-z0-9-]+$" % (
    "|".join(keys.STRANDS), "|".join(keys.SENSITIVITIES), "|".join(keys.STATES)))
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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
# Temporal coverage is a nested {"start": ..., "end": ...} object
# (r6 §3.1, the DCAT-shaped form of dcterms:temporal). Values are years
# ('1989') or ISO dates ('2025-12-31'), already checked by
# coverage_error. Comparison expands a year to its first/last day so
# ISO strings compare lexicographically; returned values keep whichever
# form the depositor gave.

def temporal_object(start, end) -> dict:
    """The nested temporal shape used at record and manifest level."""
    return {"start": start, "end": end}


def human_bytes(n) -> str:
    """1234567 -> '1.2 MB' (decimal units, one decimal above bytes)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1000.0
    return str(n)


def temporal_pair(temporal):
    """(start, end) out of a temporal object; (None, None) when absent or
    malformed - callers that need to enforce the shape (validate_record)
    do so explicitly; this one just needs to not crash on bad input."""
    if not isinstance(temporal, dict):
        return None, None
    return temporal.get("start"), temporal.get("end")


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
    """Containment check (r5 Q5): every per-file temporal range must sit
    inside the record's envelope."""
    errors = []
    start, end = temporal_pair(rec.get("temporal"))
    if not start or not end:
        return errors
    for entry in rec.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("path") or "?"
        file_start, file_end = temporal_pair(entry.get("temporal"))
        if file_start and _as_start(file_start) < _as_start(start):
            errors.append("file %r coverage starts %s, outside the dataset "
                          "envelope (%s to %s)" % (name, file_start, start, end))
        if file_end and _as_end(file_end) > _as_end(end):
            errors.append("file %r coverage ends %s, outside the dataset "
                          "envelope (%s to %s)" % (name, file_end, start, end))
    return errors


# --- manifest ----------------------------------------------------------

def manifest_entry(path_name: str, checksum_sha256: str, size_bytes: int,
                   temporal=None, fmt=None,
                   derived_from=None, notes=None) -> dict:
    """A files[] entry; empty optionals are omitted. `temporal` is the
    nested {"start", "end"} object (r6 §3.1)."""
    entry = {
        "path": path_name,
        "checksum_sha256": checksum_sha256,
        "bytes": size_bytes,
    }
    extras = (
        ("temporal", temporal),
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


# --- provenance: references and activities (r8) ------------------------

def reference_from_text(text: str) -> dict:
    """The one rule that turns what a person typed into a derived_from
    reference (r8 §1, decided). A five-part identifier is a `dataset`
    reference; anything that parses as an http(s) URL or a doi: is
    `external` with `url`; anything else is `external` with `citation`.
    Used by the CLI interview, the web form and the 0.5 upgrade path."""
    value = (text or "").strip()
    if _IDENTIFIER_RE.match(value):
        return {"kind": "dataset", "identifier": value}
    parts = urlsplit(value)
    if (parts.scheme in _URL_SCHEMES and parts.netloc) or (
            parts.scheme == "doi" and parts.path):
        return {"kind": "external", "url": value}
    return {"kind": "external", "citation": value}


def references_from_lines(text_or_list) -> List[dict]:
    """What a person typed into a "derived from" box, one reference per
    line (or one per list item): blanks dropped, each line through
    reference_from_text. The CLI interview and the web form both call
    this so the two routes cannot drift."""
    if text_or_list is None:
        return []
    if isinstance(text_or_list, str):
        items = text_or_list.splitlines()
    else:
        items = list(text_or_list)
    out = []
    for item in items:
        value = str(item or "").strip()
        if value:
            out.append(reference_from_text(value))
    return out


def validate_reference(ref, label: str,
                       own_identifier: Optional[str] = None) -> List[str]:
    """Errors for one derived_from reference; `label` names it in
    messages (e.g. 'derived_from[2]')."""
    errors = []
    if not isinstance(ref, dict):
        errors.append("%s is not an object" % label)
        return errors
    kind = ref.get("kind")
    if kind not in REFERENCE_KINDS:
        errors.append("%s: kind %r is not one of %s"
                      % (label, kind, "/".join(REFERENCE_KINDS)))
        return errors
    if kind == "dataset":
        identifier = ref.get("identifier")
        if not isinstance(identifier, str) or not _IDENTIFIER_RE.match(identifier):
            errors.append("%s: identifier %r is not a five-part dataset "
                          "identifier (strand/project/sensitivity/state/dataset)"
                          % (label, identifier))
        elif own_identifier and identifier == own_identifier:
            errors.append("%s: a dataset cannot be derived from itself" % label)
        ds_uuid = ref.get("dataset_uuid")
        if ds_uuid is not None and not _UUID4_RE.match(str(ds_uuid)):
            errors.append("%s: dataset_uuid %r is not a UUID4" % (label, ds_uuid))
        version = ref.get("version")
        if version is not None and normalise_version(str(version)) != version:
            errors.append("%s: version %r is not two integers like 1-0"
                          % (label, version))
    else:
        url = ref.get("url")
        citation = ref.get("citation")
        if not (isinstance(url, str) and url) and not (
                isinstance(citation, str) and citation):
            errors.append("%s: an external reference needs a url or a "
                          "citation" % label)
        retrieved = ref.get("retrieved")
        if retrieved is not None:
            err = coverage_error(str(retrieved))
            if err or _YEAR_RE.match(str(retrieved)):
                errors.append("%s: retrieved %r is not an ISO date"
                              % (label, retrieved))
    return errors


def validate_activity(act, label: str, manifest_paths: Set[str],
                      activity_codes: Optional[List[str]] = None
                      ) -> Tuple[List[str], List[str]]:
    """(errors, warnings) for one provenance activity (r8 §2). Outputs
    and bare-path inputs must be members of this dataset; a commit that
    is not 7-40 hex characters is a warning, nothing more."""
    errors = []
    warnings = []
    if not isinstance(act, dict):
        errors.append("%s is not an object" % label)
        return errors, warnings
    codes = list(activity_codes) if activity_codes else list(ACTIVITY_KINDS)
    if act.get("activity") not in codes:
        errors.append("%s: activity %r is not one of %s"
                      % (label, act.get("activity"), "/".join(codes)))
    for field in ("description", "agent"):
        if field in act and not isinstance(act[field], str):
            errors.append("%s: %s must be a string" % (label, field))
    tool = act.get("tool")
    if tool is not None:
        if not isinstance(tool, dict) or not tool.get("name"):
            errors.append("%s: tool must be an object with a name" % label)
        else:
            for field in ("name", "repo", "commit", "version", "command", "notebook"):
                if field in tool and not isinstance(tool[field], str):
                    errors.append("%s: tool.%s must be a string" % (label, field))
            commit = tool.get("commit")
            if isinstance(commit, str) and not _COMMIT_RE.match(commit):
                warnings.append("%s: tool.commit %r does not look like a "
                                "commit hash (7-40 hex characters)" % (label, commit))
    inputs = act.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list):
            errors.append("%s: inputs must be a list" % label)
        else:
            for i, item in enumerate(inputs):
                if isinstance(item, str):
                    if item not in manifest_paths:
                        errors.append("%s: input %r is not a member of this "
                                      "dataset" % (label, item))
                else:
                    errors.extend(validate_reference(
                        item, "%s inputs[%d]" % (label, i + 1)))
    outputs = act.get("outputs")
    if outputs is not None:
        if not isinstance(outputs, list):
            errors.append("%s: outputs must be a list" % label)
        else:
            for item in outputs:
                if not isinstance(item, str) or item not in manifest_paths:
                    errors.append("%s: output %r is not a member of this "
                                  "dataset" % (label, item))
    for field in ("started", "ended"):
        value = act.get(field)
        if value is not None and not (isinstance(value, str)
                                      and _TIMESTAMP_RE.match(value)):
            errors.append("%s: %s %r is not a UTC timestamp like "
                          "2026-09-17T08:40:00Z" % (label, field, value))
    return errors, warnings


# --- category history (r9) ---------------------------------------------

class RecordChangeError(ValueError):
    """A category change that cannot be applied to a record."""


def validate_history_entry(entry, label: str) -> List[str]:
    """Errors for one category_history entry; `label` names it."""
    errors = []
    if not isinstance(entry, dict):
        return ["%s is not an object" % label]
    when = entry.get("when")
    if not (isinstance(when, str) and _TIMESTAMP_RE.match(when)):
        errors.append("%s: when %r is not a UTC timestamp like "
                      "2026-09-24T10:00:00Z" % (label, when))
    if not isinstance(entry.get("by"), str) or not entry["by"].strip():
        errors.append("%s: by (who changed it) is missing" % label)
    kind = entry.get("kind")
    if kind not in HISTORY_KINDS:
        errors.append("%s: kind %r is not one of %s"
                      % (label, kind, "/".join(HISTORY_KINDS)))
        return errors
    for field in ("from", "to"):
        value = entry.get(field)
        if value is not None and not (isinstance(value, list)
                                      and all(isinstance(s, str) for s in value)):
            errors.append("%s: %s must be a list of strings" % (label, field))
    needs_from = kind in ("removed", "replaced", "split_review", "domain_changed")
    needs_to = kind in ("added", "replaced", "split_review", "domain_changed")
    if needs_from and not entry.get("from"):
        errors.append("%s: %s needs from" % (label, kind))
    if needs_to and not entry.get("to"):
        errors.append("%s: %s needs to" % (label, kind))
    settable = [name for name, _ in SETTABLE_FIELDS]
    if kind == "field_set":
        if entry.get("field") not in settable:
            errors.append("%s: field_set needs field, one of %s"
                          % (label, "/".join(settable)))
        if not entry.get("from") and not entry.get("to"):
            errors.append("%s: field_set needs from or to" % label)
    elif "field" in entry:
        errors.append("%s: field only belongs on a field_set entry" % label)
    for field in ("reason", "vocabulary_version"):
        if field in entry and not isinstance(entry[field], str):
            errors.append("%s: %s must be a string" % (label, field))
    return errors


def history_entry(when: str, by: str, kind: str, frm=None, to=None,
                  reason: Optional[str] = None,
                  vocabulary_version: Optional[str] = None,
                  field: Optional[str] = None) -> dict:
    entry = {"when": when, "by": by, "kind": kind}
    if field:
        entry["field"] = field
    if frm:
        entry["from"] = list(frm)
    if to:
        entry["to"] = list(to)
    if reason:
        entry["reason"] = reason
    if vocabulary_version:
        entry["vocabulary_version"] = vocabulary_version
    return entry


def provider_errors(value, provider_slugs: Optional[List[str]] = None,
                    label: str = "provider") -> Tuple[List[str], List[str]]:
    """(errors, warnings) for a `provider` value (r11 §2.2): a list of
    slugs; with a providers list in hand an unknown slug is an error,
    without one it is a warning that it could not be checked."""
    if not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
        return ["%s must be a list of provider slugs" % label], []
    errors = []
    for s in value:
        if not authority.SLUG_RE.match(s):
            errors.append("%s %r is not lower-case words joined by hyphens" % (label, s))
    if errors:
        return errors, []
    if provider_slugs:
        for s in value:
            if s not in provider_slugs:
                errors.append("%s %r is not in the vocabulary's providers list; to "
                              "propose an addition, open an issue on the "
                              "vocabulary repo" % (label, s))
        return errors, []
    if value:
        return [], ["%s could not be checked: the vocabulary in hand has no "
                    "providers list" % label]
    return [], []


def place_errors(value, place_codes: Optional[Set[str]] = None,
                 label: str = "spatial") -> Tuple[List[str], List[str]]:
    """(errors, warnings) for a `spatial` value (r11 §2.3): a list of
    place codes (`m49:NNN` or `iso:XX`); with places in hand an unknown
    code is an error, without them a warning."""
    if isinstance(value, str):
        return ["%s is a string (the 0.6 form); records read through "
                "parse_record are upgraded to a list of place codes" % label], []
    if not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
        return ["%s must be a list of place codes like m49:419 or iso:CO" % label], []
    errors = [("%s %r is not a place code like m49:419 or iso:CO" % (label, s))
              for s in value if not PLACE_CODE_RE.match(s)]
    if errors:
        return errors, []
    if place_codes:
        return [("%s %r is not a place in places.json" % (label, s))
                for s in value if s not in place_codes], []
    if value:
        return [], ["%s could not be checked: no places file in hand" % label]
    return [], []


def field_set_error(field: str, value,
                    provider_slugs: Optional[List[str]] = None,
                    place_codes: Optional[Set[str]] = None) -> Optional[str]:
    """Why one `set` pair cannot be applied, or None. `None` as the value
    clears the field. Shared by the change-file reader (so a bad file is
    refused before any record is read) and apply_dataset_change."""
    kinds = dict(SETTABLE_FIELDS)
    if field not in kinds:
        return ("%r cannot be set this way; a change file may set %s "
                "(subject by add/remove, domain by set_domain)"
                % (field, ", ".join(name for name, _ in SETTABLE_FIELDS)))
    if value is None:
        return None
    kind = kinds[field]
    if kind in ("list", "provider", "place"):
        if not (isinstance(value, list) and value
                and all(isinstance(s, str) and s.strip() for s in value)):
            return "%s must be a list of non-empty strings, or null to clear it" % field
        if kind == "provider":
            errors, _ = provider_errors(value, provider_slugs)
        elif kind == "place":
            errors, _ = place_errors(value, place_codes)
        else:
            errors = []
        return errors[0] if errors else None
    if not isinstance(value, str) or not value.strip():
        return "%s must be text, or null to clear it" % field
    if kind == "source_type" and value not in SOURCE_TYPES:
        return "source_type %r is not one of %s" % (value, "/".join(SOURCE_TYPES))
    return None


def domain_list(value) -> List[str]:
    """A record's or interview's domain as the 0.7 list: a bare code
    (0.6, or a flag) becomes a one-element list; None becomes []."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [d for d in value]


def _as_list(value) -> Optional[List[str]]:
    if value is None:
        return None
    return list(value) if isinstance(value, list) else [value]


def apply_dataset_change(rec: dict, change: dict, who: str, now: str,
                         vocab_terms: Set[str],
                         domain_codes: Optional[List[str]] = None,
                         vocabulary_version: Optional[str] = None,
                         provider_slugs: Optional[List[str]] = None,
                         place_codes: Optional[Set[str]] = None
                         ) -> Tuple[dict, List[dict]]:
    """One steward decision about one dataset (r9 §4, r11 §1): `change`
    may carry `add` and `remove` (subject terms), `set_domain` (a code
    or a list, primary first), `set` (descriptive fields, see
    SETTABLE_FIELDS; a null value clears) and `reason`. Returns (new
    record, the history entries appended). Unknown terms, unknown
    domains, removing a term the record does not have, leaving no
    subject at all, or setting a field that cannot be set this way raise
    RecordChangeError; the input record is untouched. `modified` moves
    to `now`; `vocabulary_version` is stamped when given. A change that
    changes nothing returns the record as it was and no entries."""
    add = [s for s in (change.get("add") or [])]
    remove = [s for s in (change.get("remove") or [])]
    domain = change.get("set_domain")
    sets = change.get("set") or {}
    reason = change.get("reason") or None
    unknown = unknown_subjects(add, vocab_terms)
    if unknown:
        raise RecordChangeError("not in the vocabulary: %s" % ", ".join(unknown))
    subjects = list(rec.get("subject") or [])
    missing = [s for s in remove if s not in subjects]
    if missing:
        raise RecordChangeError("not on this record, so cannot be removed: %s"
                                % ", ".join(missing))
    domains = None
    if domain is not None:
        codes = list(domain_codes) if domain_codes else list(keys.DOMAINS)
        domains = domain_list(domain)
        bad = [d for d in domains if d not in codes]
        if bad or not domains:
            raise RecordChangeError("domain %r is not one of %s"
                                    % (bad[0] if bad else domain, "/".join(codes)))
        if len(set(domains)) != len(domains):
            raise RecordChangeError("set_domain repeats a code: %r" % (domain,))
    for field, value in sets.items():
        err = field_set_error(field, value, provider_slugs, place_codes)
        if err:
            raise RecordChangeError(err)
    new_subjects = [s for s in subjects if s not in remove]
    for s in add:
        if s not in new_subjects:
            new_subjects.append(s)
    if not new_subjects:
        raise RecordChangeError("a dataset must keep at least one subject term")

    entries = []
    really_removed = [s for s in remove if s in subjects]
    really_added = [s for s in add if s not in subjects]
    if really_removed:
        entries.append(history_entry(now, who, "removed", frm=really_removed,
                                     reason=reason,
                                     vocabulary_version=vocabulary_version))
    if really_added:
        entries.append(history_entry(now, who, "added", to=really_added,
                                     reason=reason,
                                     vocabulary_version=vocabulary_version))
    current_domains = domain_list(rec.get("domain"))
    if domains is not None and domains != current_domains:
        entries.append(history_entry(now, who, "domain_changed",
                                     frm=current_domains, to=domains,
                                     reason=reason,
                                     vocabulary_version=vocabulary_version))
    # One entry per field, in record order whatever order the file used;
    # a value the record already has (or clearing an absent field) is
    # nothing to record.
    really_set = []
    for field, _kind in SETTABLE_FIELDS:
        if field not in sets or sets[field] == rec.get(field):
            continue
        really_set.append(field)
        entries.append(history_entry(now, who, "field_set", field=field,
                                     frm=_as_list(rec.get(field)),
                                     to=_as_list(sets[field]), reason=reason,
                                     vocabulary_version=vocabulary_version))
    if not entries:
        return rec, []
    out = dict(rec)
    out["subject"] = new_subjects
    if domains is not None:
        out["domain"] = domains
    for field in really_set:
        if sets[field] is None:
            out.pop(field, None)
        else:
            out[field] = sets[field]
    out["modified"] = now
    if vocabulary_version:
        out["vocabulary_version"] = vocabulary_version
    out["category_history"] = list(rec.get("category_history") or []) + entries
    return _in_field_order(out), entries


def apply_vocabulary_mapping(rec: dict, vocab_doc: dict, now: str
                             ) -> Tuple[dict, List[dict]]:
    """The record with its subjects brought up to the vocabulary's
    current terms (r9 §2 and §3), recorded as history entries by the
    actor "vocabulary". A record whose terms are all current comes back
    as it was, with no entries, so this is safe to run on every record
    on every run."""
    subjects, applied = authority.map_subjects(
        list(rec.get("subject") or []), vocab_doc)
    if not applied:
        return rec, []
    version = vocab_doc.get("vocabulary_version")
    entries = [history_entry(now, VOCABULARY_ACTOR, a["kind"],
                             frm=a.get("from"), to=a.get("to"),
                             reason="vocabulary change dated %s" % a.get("date"),
                             vocabulary_version=version)
               for a in applied]
    out = dict(rec)
    out["subject"] = subjects
    out["modified"] = now
    if version:
        out["vocabulary_version"] = version
    out["category_history"] = list(rec.get("category_history") or []) + entries
    return _in_field_order(out), entries


def _in_field_order(rec: dict) -> dict:
    """The record's keys in build_record order, so a rewritten record
    diffs cleanly against the original."""
    order = ["schema_version", "dataset_uuid", "identifier", "strand",
             "project", "dataset", "sensitivity", "state", "domain",
             "version", "abstract", "subject", "vocabulary_version",
             "temporal", "creator", "source_type", "source_detail",
             "provider", "license", "steward", "depositors", "created",
             "modified"] + list(OPTIONAL_FIELDS) + ["files"]
    out = {k: rec[k] for k in order if k in rec}
    for k in rec:
        if k not in out:
            out[k] = rec[k]
    return out


def _spatial_text(value) -> str:
    """The 0.6 free-text `spatial` as one line for `spatial_note`."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return json.dumps(value, ensure_ascii=False) if value not in (None, {}, "") else ""


def _with_spatial_note(rec: dict, text: str) -> dict:
    """`spatial` (the 0.6 form) replaced in place by `spatial_note`, so
    the upgraded record keeps its field order."""
    note = rec.get("spatial_note")
    if isinstance(note, str) and note.strip():
        text = note.strip() + "; " + text
    out = {}
    for k, v in rec.items():
        if k == "spatial":
            out["spatial_note"] = text
        elif k != "spatial_note":
            out[k] = v
    return out


def upgrade_record(rec: dict) -> Tuple[dict, bool]:
    """Bring a record read from storage up to SCHEMA_VERSION. Returns
    (record, upgraded). A current record is returned untouched, which
    the write-then-read-back checks rely on. The steps run in order, so
    a 0.5 record goes 0.5 -> 0.6 -> 0.7 in one call.
    0.5 -> 0.6: the string derived_from becomes one reference (r8 §5);
    files[].derived_from stays a string.
    0.6 -> 0.7 (r11 §2.5): the string domain becomes a one-element list;
    a free-text spatial moves to spatial_note and the key goes; a list
    of place codes is kept as it is.
    Anything else raises RecordParseError."""
    version = rec.get("schema_version")
    if version == SCHEMA_VERSION:
        return rec, False
    if version not in ACCEPTED_SCHEMA_VERSIONS:
        raise RecordParseError(
            "schema_version %r is not one of %s; refusing to modify a record "
            "this version of the tool does not understand"
            % (version, "/".join(ACCEPTED_SCHEMA_VERSIONS)))
    if version == "0.5":
        derived = rec.get("derived_from")
        if isinstance(derived, str):
            rec["derived_from"] = [reference_from_text(derived)] if derived.strip() else []
            if not rec["derived_from"]:
                del rec["derived_from"]
        version = "0.6"
    if version == "0.6":
        domain = rec.get("domain")
        if isinstance(domain, str) and domain.strip():
            rec["domain"] = [domain]
        if "spatial" in rec:
            spatial = rec["spatial"]
            coded = (isinstance(spatial, list) and spatial
                     and all(isinstance(s, str) and PLACE_CODE_RE.match(s)
                             for s in spatial))
            if not coded:
                text = _spatial_text(spatial)
                if text:
                    rec = _with_spatial_note(rec, text)
                else:
                    del rec["spatial"]
        version = "0.7"
    rec["schema_version"] = SCHEMA_VERSION
    return rec, True


# --- record assembly and validation ------------------------------------

def build_record(dataset_uuid, identifier, strand, domain, project, dataset,
                 state, sensitivity, temporal, version, abstract,
                 subject, files, created, modified, vocabulary_version=None,
                 creator=None, source_type=None, source_detail=None,
                 provider=None, license=None, steward=None, depositors=None,
                 derived_from=None, provenance=None, category_history=None,
                 language=None, ethics_ref=None, spatial=None,
                 spatial_note=None, notes=None) -> dict:
    """Assemble the dataset record in field order.
    Recommended/optional fields that are None or empty are omitted.
    `temporal` is the nested {"start", "end"} object; `subject` and
    `license` carry the Dublin Core spellings (r6 §3.1). `derived_from`
    is a list of references and `provenance` a list of activities (r8).
    `domain` is a list, primary first (a bare code is wrapped);
    `provider` a list of provider slugs and `spatial` a list of place
    codes (r11)."""
    rec = {
        "schema_version": SCHEMA_VERSION,
        "dataset_uuid": dataset_uuid,
        "identifier": identifier,
        "strand": strand,
        "project": project,
        "dataset": dataset,
        "sensitivity": sensitivity,
        "state": state,
        "domain": domain_list(domain),
        "version": version,
        "abstract": abstract,
        "subject": list(subject),
    }
    if vocabulary_version:
        rec["vocabulary_version"] = vocabulary_version
    rec["temporal"] = dict(temporal)
    for name, value in (
            ("creator", creator), ("source_type", source_type),
            ("source_detail", source_detail), ("provider", provider),
            ("license", license), ("steward", steward),
            ("depositors", depositors)):
        if value:
            rec[name] = value
    rec["created"] = created
    rec["modified"] = modified
    for name, value in (
            ("derived_from", derived_from), ("provenance", provenance),
            ("category_history", category_history),
            ("language", language), ("ethics_ref", ethics_ref),
            ("spatial", spatial), ("spatial_note", spatial_note),
            ("notes", notes)):
        if value:
            rec[name] = value
    rec["files"] = list(files)
    return rec


def validate_record(rec: dict, vocab_terms: Set[str],
                    domain_codes: Optional[List[str]] = None,
                    activity_codes: Optional[List[str]] = None,
                    provider_slugs: Optional[List[str]] = None,
                    place_codes: Optional[Set[str]] = None
                    ) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block a deposit; warnings do not.
    Hand-rolled on purpose: the shipped tool cannot use jsonschema (stdlib
    constraint) — dataset.schema.json is the contract artefact for CI and
    the gateway, this function is the runtime check (r5 §7).
    domain_codes / activity_codes: valid codes from the fetched vocabulary;
    None falls back to the built-in lists (r2 §2 - fetched, not hardcoded).
    provider_slugs / place_codes (r11): the providers list and the
    places in hand; None or empty means an unknown value is a warning,
    not an error, so an old cache never blocks a deposit."""
    errors = []
    warnings = []
    for field in REQUIRED_FIELDS:
        if field not in rec or rec[field] in (None, "", [], {}):
            errors.append("required field '%s' is missing or empty" % field)
    if errors:
        return errors, warnings

    if rec["strand"] not in keys.STRANDS:
        errors.append("strand %r is not one of %s"
                      % (rec["strand"], "/".join(keys.STRANDS)))
    valid_domains = list(domain_codes) if domain_codes else list(keys.DOMAINS)
    domain = rec["domain"]
    if isinstance(domain, str):
        errors.append("domain is a string (the 0.6 form); records read through "
                      "parse_record are upgraded to a list, primary first")
    elif not isinstance(domain, list) or not all(isinstance(d, str) for d in domain):
        errors.append("domain must be a list of domain codes, primary first")
    else:
        for d in domain:
            if d not in valid_domains:
                errors.append("domain %r is not one of %s"
                              % (d, "/".join(valid_domains)))
        if len(set(domain)) != len(domain):
            errors.append("domain repeats a code")
    if rec["state"] not in keys.STATES:
        errors.append("state %r is not one of %s"
                      % (rec["state"], "/".join(keys.STATES)))
    if rec["sensitivity"] not in keys.SENSITIVITIES:
        errors.append("sensitivity %r is not green or amber" % rec["sensitivity"])
    if not keys.validate_project(str(rec["dataset"])):
        errors.append("dataset %r is not a valid slug (lowercase "
                      "letters/digits and hyphens)" % (rec["dataset"],))

    # r6 §2: the identifier is the five-part prefix and its final
    # element must equal the dataset field - a mismatch means the record
    # was moved or hand-edited, and is an error, not a warning.
    expected_id = "/".join((rec["strand"], rec["project"],
                            rec["sensitivity"], rec["state"],
                            rec["dataset"]))
    if rec["identifier"] != expected_id:
        errors.append("identifier %r does not match the path parts (%r)"
                      % (rec["identifier"], expected_id))
    if not _UUID4_RE.match(str(rec["dataset_uuid"])):
        errors.append("dataset_uuid %r is not a UUID4" % (rec["dataset_uuid"],))

    if not isinstance(rec["temporal"], dict):
        errors.append("'temporal' must be an object with start and end")
    else:
        for part in ("start", "end"):
            err = coverage_error(rec["temporal"].get(part))
            if err:
                errors.append("temporal %s: %s" % (part, err))

    if not isinstance(rec["subject"], list) or not rec["subject"]:
        errors.append("at least one subject term is required")
    else:
        for term in unknown_subjects(rec["subject"], vocab_terms):
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
    for field in ("creator", "source_detail", "license", "steward",
                  "notes", "ethics_ref", "spatial_note"):
        if field in rec and not isinstance(rec[field], str):
            errors.append("'%s' must be a string" % field)
    if "source_type" in rec and rec["source_type"] not in SOURCE_TYPES:
        errors.append("source_type %r is not one of %s"
                      % (rec["source_type"], "/".join(SOURCE_TYPES)))
    # r11: provider slugs and place codes, against the lists in hand.
    if "provider" in rec:
        more_errors, more_warnings = provider_errors(rec["provider"], provider_slugs)
        errors.extend(more_errors)
        warnings.extend(more_warnings)
    if "spatial" in rec:
        more_errors, more_warnings = place_errors(rec["spatial"], place_codes)
        errors.extend(more_errors)
        warnings.extend(more_warnings)

    if "license" not in rec and "licence" in rec:
        errors.append("'licence' was renamed 'license' in v0.5 (the "
                      "Dublin Core spelling)")

    # r8 §1: derived_from is a list of references. A bare string is the
    # 0.5 shape arriving unupgraded (parse_record would have converted it).
    if "derived_from" in rec:
        derived = rec["derived_from"]
        if isinstance(derived, str):
            errors.append("derived_from is a string (the 0.5 form); records "
                          "read through parse_record are upgraded to a list "
                          "of references")
        elif not isinstance(derived, list):
            errors.append("derived_from must be a list of references")
        else:
            for i, ref in enumerate(derived):
                errors.extend(validate_reference(
                    ref, "derived_from[%d]" % (i + 1), rec["identifier"]))
    if rec.get("source_type") == "derived" and not rec.get("derived_from"):
        warnings.append("source type is 'derived' but derived_from is empty "
                        "- derived from what?")

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
        if path:
            structural = keys.member_path_error(path)
            if structural:
                errors.append("manifest path %s" % structural)
        if path and keys.is_reserved_member(path):
            errors.append("manifest path %r matches the reserved "
                          "dataset.*.json pattern (at any depth)" % path)
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
        file_temporal = entry.get("temporal")
        if file_temporal is not None:
            if not isinstance(file_temporal, dict):
                errors.append("file %s: temporal must be an object with "
                              "start and end" % name)
            else:
                for part in ("start", "end"):
                    err = coverage_error(file_temporal.get(part))
                    if err:
                        errors.append("file %s temporal %s: %s"
                                      % (name, part, err))
    errors.extend(envelope_errors(rec))

    # r8 §2: provenance activities, checked against the manifest paths.
    if "provenance" in rec:
        if not isinstance(rec["provenance"], list):
            errors.append("provenance must be a list of activities")
        else:
            for i, act in enumerate(rec["provenance"]):
                act_errors, act_warnings = validate_activity(
                    act, "provenance[%d]" % (i + 1), seen_paths, activity_codes)
                errors.extend(act_errors)
                warnings.extend(act_warnings)
    # r9: category history entries.
    if "category_history" in rec:
        if not isinstance(rec["category_history"], list):
            errors.append("category_history must be a list of entries")
        else:
            for i, entry in enumerate(rec["category_history"]):
                errors.extend(validate_history_entry(
                    entry, "category_history[%d]" % (i + 1)))
    return errors, warnings


def record_json(rec: dict) -> str:
    return json.dumps(rec, indent=2, ensure_ascii=False) + "\n"


def parse_record_with_status(text: str) -> Tuple[dict, bool]:
    """Parse an existing dataset record fetched from storage and bring it
    up to SCHEMA_VERSION; returns (record, upgraded) so callers that log
    can say a 0.5 record was converted. Raises RecordParseError for
    anything the tool must not build on: invalid JSON, a non-object, a
    schema_version it does not understand (including newer ones — never
    blind-overwrite the future), or a missing/empty files manifest.
    Absence-vs-fetch-error is the transfer layer's distinction, not this
    function's."""
    try:
        rec = json.loads(text)
    except ValueError as e:
        raise RecordParseError("not valid JSON: %s" % e)
    if not isinstance(rec, dict):
        raise RecordParseError("not a JSON object")
    rec, upgraded = upgrade_record(rec)
    files = rec.get("files")
    if not isinstance(files, list) or not files:
        raise RecordParseError("the files manifest is missing or empty")
    return rec, upgraded


def parse_record(text: str) -> dict:
    """parse_record_with_status without the flag; the common call."""
    return parse_record_with_status(text)[0]
