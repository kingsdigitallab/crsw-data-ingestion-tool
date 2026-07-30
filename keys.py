"""Object key construction and filename checks for CRSW deposits.

Kept separate from sidecar.py deliberately: the path convention
(handbook v0.4 §4) may change independently of the metadata schema.
Object keys ALWAYS use '/' regardless of platform.
"""
import re
from typing import List

STRANDS = ("rs1", "rs2", "rs3", "rs4")

# Member filenames matching this pattern are reserved for dataset
# records (r6 §2): the record is dataset.<slug>.json, so the whole
# dataset.*.json shape is refused as a member name.
RESERVED_RECORD_RE = re.compile(r"^dataset\..*\.json$")
DOMAINS = ("quant", "geo", "pol", "narr", "parti")
STATES = ("0_raw", "1_interim", "2_final")
SENSITIVITIES = ("green", "amber")

PROBLEM_CHARS = '\\/:*?"<>|'

_PROJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NON_PROJECT_RE = re.compile(r"[^a-z0-9-]")


class RedDataError(ValueError):
    """Raised when sensitivity 'red' reaches key construction. Red data
    never enters shared storage (handbook §3, §7.3)."""


def validate_project(project: str) -> bool:
    """Lowercase letters/digits, hyphen-separated. ASCII only.
    The same slug rule covers project and dataset names (r6 §1)."""
    return bool(_PROJECT_RE.match(project))


def filename_problems(filename: str) -> List[str]:
    """Return human-readable problems with a filename, empty if clean.

    Flags exactly the spec §4 set: spaces and \\ / : * ? " < > |.
    Anything else is preserved — filenames are kept as deposited."""
    problems = []
    if " " in filename:
        problems.append("contains spaces")
    bad = sorted(set(c for c in filename if c in PROBLEM_CHARS))
    for c in bad:
        problems.append("contains '%s'" % c)
    return problems


def suggest_filename(filename: str) -> str:
    """Suggested correction: spaces to hyphens, problem chars stripped.
    Never applied automatically — the user must accept or edit it."""
    out = filename.replace(" ", "-")
    return "".join(c for c in out if c not in PROBLEM_CHARS)


def normalise_project(name: str) -> str:
    """Normalise a proposed project or dataset name: lowercase,
    spaces to hyphens, anything outside [a-z0-9-] stripped, repeated
    hyphens collapsed, edge hyphens trimmed.

    A non-empty result always passes validate_project(); an empty result
    means nothing usable remained."""
    out = _NON_PROJECT_RE.sub("", name.lower().replace(" ", "-"))
    return re.sub("-{2,}", "-", out).strip("-")


def similar_projects(name: str, existing: List[str]) -> List[str]:
    """Existing project names that near-match a normalised candidate:
    equal after case-folding and hyphen removal, substring either way, or
    differing only by a trailing 's'. Exact matches are excluded — the
    caller handles "already exists" separately. Deliberately not clever
    (r4 §2): this catches the near-duplicates that actually happen."""
    a = name.replace("-", "")
    matches = []
    for entry in existing:
        if entry == name:
            continue
        b = entry.lower().replace("-", "")
        if a == b or a in b or b in a or a.rstrip("s") == b.rstrip("s"):
            matches.append(entry)
    return matches


def is_reserved_name(filename: str) -> bool:
    """True for member filenames reserved for dataset records — the
    dataset.*.json pattern, not a single fixed name (r6 §2)."""
    return bool(RESERVED_RECORD_RE.match(filename or ""))


def record_filename(dataset: str) -> str:
    """The record filename for a dataset: dataset.<slug>.json. The slug
    always equals the prefix's final path element, so any consumer can
    build the record key from the prefix alone, no listing (r6 §2)."""
    return "dataset.%s.json" % dataset


def _validate_parts(strand: str, project: str, sensitivity: str,
                    state: str, dataset: str) -> None:
    """Raise RedDataError for sensitivity 'red', ValueError for any other
    invalid path part. Backstop — deposit.py refuses red earlier."""
    if sensitivity == "red":
        raise RedDataError(
            "red data must not enter shared storage; it belongs in the TRE")
    if strand not in STRANDS:
        raise ValueError("strand must be one of %s, got %r" % ("/".join(STRANDS), strand))
    if state not in STATES:
        raise ValueError("state must be one of %s, got %r" % ("/".join(STATES), state))
    if sensitivity not in SENSITIVITIES:
        raise ValueError("sensitivity must be green or amber, got %r" % sensitivity)
    if not validate_project(project):
        raise ValueError(
            "project must be lowercase letters/digits with hyphens, got %r" % project)
    if not validate_project(dataset):
        raise ValueError(
            "dataset must be lowercase letters/digits with hyphens, got %r" % dataset)


def dataset_prefix(strand: str, project: str, sensitivity: str,
                   state: str, dataset: str) -> str:
    """The dataset prefix {strand}/{project}/{sensitivity}/{state}/{dataset}
    (r6 §1 — the dataset is a real path element; a project may hold
    several). This string is also the record's `identifier` field."""
    _validate_parts(strand, project, sensitivity, state, dataset)
    return "/".join((strand, project, sensitivity, state, dataset))


def record_key(strand: str, project: str, sensitivity: str,
               state: str, dataset: str) -> str:
    """The dataset record's object key: the prefix + dataset.<slug>.json."""
    return (dataset_prefix(strand, project, sensitivity, state, dataset)
            + "/" + record_filename(dataset))


def build_key(strand: str, project: str, sensitivity: str, state: str,
              dataset: str, filename: str) -> str:
    """Build the object key
    {strand}/{project}/{sensitivity}/{state}/{dataset}/{filename}.

    Raises RedDataError for sensitivity 'red', ValueError for any other
    invalid part or for a reserved record filename. Backstops —
    deposit.py refuses both earlier."""
    _validate_parts(strand, project, sensitivity, state, dataset)
    if not filename:
        raise ValueError("filename must not be empty")
    if is_reserved_name(filename):
        raise ValueError(
            "%r matches dataset.*.json, which is reserved for dataset "
            "records" % filename)
    return "/".join((strand, project, sensitivity, state, dataset, filename))
