"""Object key construction and filename checks for CRSW deposits.

Kept separate from record.py deliberately: the path convention
(handbook v0.4 §4) may change independently of the metadata schema.
Object keys ALWAYS use '/' regardless of platform.
"""
import re
import unicodedata
from typing import List, Optional

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


# --- member paths (r7 §1: folders as deposit arguments) ----------------
# A "member path" is a filename that may contain '/' to preserve the
# sub-path a file had relative to the argument's root (e.g. a folder
# deposit). It is what becomes the object key's last element AND the
# manifest `path` field - the same string serves both, which is why it
# is validated in one place rather than at each use.

MEMBER_SEGMENT_MAX_BYTES = 255
MEMBER_PATH_MAX_BYTES = 1024  # the S3 key-length limit, checked on the
                              # full key by the caller; here on the member
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def normalise_member_path(parts) -> str:
    """Join path segments (e.g. Path.parts, already relative to the
    argument's root) with '/', each NFC-normalised. The local file on
    disk is never touched - this only affects the object key and the
    manifest path. macOS hands back decomposed (NFD) Unicode where
    Windows/Linux hand back composed (NFC); without this, the same
    accented filename deposited from a Mac and a PC produces two
    manifest entries and two objects for identical content (r7 §4)."""
    return "/".join(unicodedata.normalize("NFC", p) for p in parts)


def member_path_error(member: str) -> Optional[str]:
    """Structural problems that make a member path unusable as an object
    key - refused outright, never offered as 'keep as-is'. This is about
    the key/manifest grammar itself, not consumer-filesystem friendliness
    (that is member_path_problems, advisory). None if the path is clean."""
    if not member:
        return "member path is empty"
    if len(member.encode("utf-8")) > MEMBER_PATH_MAX_BYTES:
        return "member path is over %d bytes" % MEMBER_PATH_MAX_BYTES
    if "\\" in member:
        return ("%r contains a backslash - object keys always use '/'"
                % member)
    if member.startswith("/") or member.endswith("/"):
        return "%r starts or ends with '/'" % member
    if _DRIVE_RE.match(member) or member.startswith("//"):
        return "%r looks like an absolute or drive-rooted path" % member
    for seg in member.split("/"):
        if not seg:
            return "%r has an empty segment ('//')" % member
        if seg in (".", ".."):
            return "%r contains a '%s' segment" % (member, seg)
        if not seg.strip():
            return "%r has a whitespace-only segment" % member
        if any(ord(c) < 0x20 for c in seg):
            return "%r contains a control character" % member
        if len(seg.encode("utf-8")) > MEMBER_SEGMENT_MAX_BYTES:
            return ("%r has a segment over %d bytes"
                    % (member, MEMBER_SEGMENT_MAX_BYTES))
    return None


def member_path_problems(member: str) -> List[str]:
    """Advisory, per-segment problems (the spec §4 set, same rules as
    filename_problems) - offered as a correction, never blocking. Must
    run per segment: PROBLEM_CHARS includes '/', so running the plain
    filename check on a whole member path would flag every separator."""
    problems = []
    for seg in member.split("/"):
        for p in filename_problems(seg):
            problems.append("%r %s" % (seg, p))
    return problems


def suggest_member_path(member: str) -> str:
    """Suggested correction for a member path: each segment run through
    suggest_filename, rejoined. Never applied automatically."""
    return "/".join(suggest_filename(seg) for seg in member.split("/"))


def is_reserved_member(member: str) -> bool:
    """True if ANY segment of a member path matches dataset.*.json - not
    just the last. The reservation is a pattern, not a location (r6 §2):
    since the dataset is a real path element, a nested dataset.sub.json
    looks exactly like the record of a dataset called 'sub' to any
    consumer walking the tree."""
    return any(is_reserved_name(seg) for seg in member.split("/"))


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


def dataset_prefix_ok(identifier: str) -> bool:
    """True when `identifier` is a well-formed five-part dataset prefix
    (strand/project/sensitivity/state/dataset); no store access."""
    parts = (identifier or "").split("/")
    if len(parts) != 5:
        return False
    strand, project, sensitivity, state, dataset = parts
    return (strand in STRANDS and sensitivity in SENSITIVITIES and state in STATES
            and bool(validate_project(project)) and bool(validate_project(dataset)))


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
              dataset: str, member: str) -> str:
    """Build the object key
    {strand}/{project}/{sensitivity}/{state}/{dataset}/{member}.

    `member` may contain '/' to preserve a sub-path relative to the
    argument's root (r7 §1, folders as deposit arguments) - it is
    validated here, the one place the "never let os.sep leak into a key"
    invariant can be guaranteed for every caller, including the future
    gateway which never goes near deposit.py.

    Raises RedDataError for sensitivity 'red', ValueError for any other
    invalid part, a structural member-path problem (member_path_error),
    or a reserved record name at any depth (is_reserved_member).
    Backstops — deposit.py refuses these earlier."""
    _validate_parts(strand, project, sensitivity, state, dataset)
    error = member_path_error(member)
    if error:
        raise ValueError(error)
    if is_reserved_member(member):
        raise ValueError(
            "%r matches dataset.*.json, which is reserved for dataset "
            "records" % member)
    return "/".join((strand, project, sensitivity, state, dataset, member))
