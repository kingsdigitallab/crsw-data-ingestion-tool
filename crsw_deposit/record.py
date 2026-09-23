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

from . import keys

# Every tool writes SCHEMA_VERSION. parse_record also reads the older
# versions in ACCEPTED_SCHEMA_VERSIONS and upgrades them on the way in
# (r8 §5): nobody converts a record by hand.
SCHEMA_VERSION = "0.6"
ACCEPTED_SCHEMA_VERSIONS = ("0.5", "0.6")

REQUIRED_FIELDS = (
    "schema_version", "dataset_uuid", "identifier", "strand", "domain",
    "project", "dataset", "state", "sensitivity", "temporal",
    "version", "abstract", "subject", "created", "modified", "files",
)
RECOMMENDED_FIELDS = (
    "vocabulary_version", "creator", "source_type", "source_detail",
    "license", "steward", "depositors",
)
OPTIONAL_FIELDS = ("derived_from", "provenance", "language", "ethics_ref",
                   "spatial", "notes")

MANIFEST_REQUIRED = ("path", "checksum_sha256", "bytes")
MANIFEST_OPTIONAL = ("temporal", "format", "derived_from", "notes")

# r8 §1: a derived_from reference is a dataset in the store or something
# outside it. r8 §2: an activity is one transformation step; the kinds
# are vocabulary-managed (vocab.activity_codes) with this fallback.
REFERENCE_KINDS = ("dataset", "external")
ACTIVITY_KINDS = ("convert", "clean", "harmonise", "aggregate", "geocode",
                  "anonymise", "merge", "subset", "manual", "other")
_URL_SCHEMES = ("http", "https")

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


def upgrade_record(rec: dict) -> Tuple[dict, bool]:
    """Bring a record read from storage up to SCHEMA_VERSION in place.
    Returns (record, upgraded). A current record is returned untouched,
    which the write-then-read-back checks rely on. 0.5 -> 0.6: the
    string derived_from becomes one reference (r8 §5); files[].derived_from
    stays a string. Anything else raises RecordParseError."""
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
        rec["schema_version"] = "0.6"
    return rec, True


# --- record assembly and validation ------------------------------------

def build_record(dataset_uuid, identifier, strand, domain, project, dataset,
                 state, sensitivity, temporal, version, abstract,
                 subject, files, created, modified, vocabulary_version=None,
                 creator=None, source_type=None, source_detail=None,
                 license=None, steward=None, depositors=None,
                 derived_from=None, provenance=None, language=None,
                 ethics_ref=None, spatial=None, notes=None) -> dict:
    """Assemble the dataset record in field order.
    Recommended/optional fields that are None or empty are omitted.
    `temporal` is the nested {"start", "end"} object; `subject` and
    `license` carry the Dublin Core spellings (r6 §3.1). `derived_from`
    is a list of references and `provenance` a list of activities (r8)."""
    rec = {
        "schema_version": SCHEMA_VERSION,
        "dataset_uuid": dataset_uuid,
        "identifier": identifier,
        "strand": strand,
        "project": project,
        "dataset": dataset,
        "sensitivity": sensitivity,
        "state": state,
        "domain": domain,
        "version": version,
        "abstract": abstract,
        "subject": list(subject),
    }
    if vocabulary_version:
        rec["vocabulary_version"] = vocabulary_version
    rec["temporal"] = dict(temporal)
    for name, value in (
            ("creator", creator), ("source_type", source_type),
            ("source_detail", source_detail), ("license", license),
            ("steward", steward), ("depositors", depositors)):
        if value:
            rec[name] = value
    rec["created"] = created
    rec["modified"] = modified
    for name, value in (
            ("derived_from", derived_from), ("provenance", provenance),
            ("language", language), ("ethics_ref", ethics_ref),
            ("spatial", spatial), ("notes", notes)):
        if value:
            rec[name] = value
    rec["files"] = list(files)
    return rec


def validate_record(rec: dict, vocab_terms: Set[str],
                    domain_codes: Optional[List[str]] = None,
                    activity_codes: Optional[List[str]] = None
                    ) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block a deposit; warnings do not.
    Hand-rolled on purpose: the shipped tool cannot use jsonschema (stdlib
    constraint) — dataset.schema.json is the contract artefact for CI and
    the gateway, this function is the runtime check (r5 §7).
    domain_codes / activity_codes: valid codes from the fetched vocabulary;
    None falls back to the built-in lists (r2 §2 - fetched, not hardcoded)."""
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
    if rec["domain"] not in valid_domains:
        errors.append("domain %r is not one of %s"
                      % (rec["domain"], "/".join(valid_domains)))
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
