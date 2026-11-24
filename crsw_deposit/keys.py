"""Object key construction and filename checks for CRSW deposits.

Object keys always use '/', on every platform.
"""
import re
import unicodedata
from typing import List, Optional

STRANDS = ("rs1", "rs2", "rs3", "rs4", "centre")
# The strands a person may deposit to through the form or the CLI.
# `centre` is Centre-curated and written only by the bulk route
# (crsw_web.bulk); search and the promoter read every strand.
DEPOSIT_STRANDS = ("rs1", "rs2", "rs3", "rs4")

# dataset.*.json is the reserved record name: the record is
# dataset.<slug>.json, so no member may have a name of that shape.
RESERVED_RECORD_RE = re.compile(r"^dataset\..*\.json$")
DOMAINS = ("quant", "geo", "pol", "narr", "parti")
STATES = ("0_raw", "1_interim", "2_final")
SENSITIVITIES = ("green", "amber")

PROBLEM_CHARS = '\\/:*?"<>|'

_PROJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NON_PROJECT_RE = re.compile(r"[^a-z0-9-]")


class RedDataError(ValueError):
    """Raised when sensitivity 'red' reaches key construction. Red data
    never enters shared storage."""


def validate_project(project: str) -> bool:
    """True if the name is a valid project or dataset slug: lowercase
    ASCII letters and digits, hyphen-separated."""
    return bool(_PROJECT_RE.match(project))


def filename_problems(filename: str) -> List[str]:
    """Return human-readable problems with a filename, empty if clean.
    Flags spaces and \\ / : * ? " < > | and nothing else."""
    # Any other character is kept as deposited.
    problems = []
    if " " in filename:
        problems.append("contains spaces")
    bad = sorted(set(c for c in filename if c in PROBLEM_CHARS))
    for c in bad:
        problems.append("contains '%s'" % c)
    return problems


def suggest_filename(filename: str) -> str:
    """Return a suggested correction: spaces to hyphens, problem
    characters removed. Never applied automatically; the user accepts or
    edits it."""
    out = filename.replace(" ", "-")
    return "".join(c for c in out if c not in PROBLEM_CHARS)


def normalise_project(name: str) -> str:
    """Return a proposed project or dataset name as a slug: lowercase,
    spaces to hyphens, other characters removed, hyphens collapsed and
    trimmed. A non-empty result always passes validate_project(); an empty one
    means nothing usable remained."""
    out = _NON_PROJECT_RE.sub("", name.lower().replace(" ", "-"))
    return re.sub("-{2,}", "-", out).strip("-")


def similar_projects(name: str, existing: List[str]) -> List[str]:
    """Return the existing names that nearly match a normalised
    candidate: equal ignoring case and hyphens, one containing the other,
    or differing only by a trailing 's'. An exact match is not returned."""
    # Simple on purpose: these are the near-duplicates that actually happen.
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
    """True if a filename is the reserved record name (dataset.*.json)."""
    return bool(RESERVED_RECORD_RE.match(filename or ""))


# --- member paths ------------------------------------------------------
# A "member path" is a filename that may contain '/', preserving the
# sub-path a file had relative to the deposit argument's root. The same
# string is the object key's last element and the manifest `path` field,
# so it is validated in one place.

MEMBER_SEGMENT_MAX_BYTES = 255
MEMBER_PATH_MAX_BYTES = 1024  # the S3 key-length limit, checked on the
                              # full key by the caller; here on the member
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def normalise_member_path(parts) -> str:
    """Join path segments with '/' and return the member path, each
    segment in composed (NFC) Unicode. The file on disk is never renamed."""
    # Without the NFC step one accented filename deposited from a Mac and
    # from a PC becomes two members.
    return "/".join(unicodedata.normalize("NFC", p) for p in parts)


def member_path_error(member: str) -> Optional[str]:
    """Return why a member path cannot be used in an object key, or None
    if it can. These are refusals, never offered as 'keep as-is'; the
    advisory checks are in member_path_problems."""
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
    """Return advisory problems with a member path, by the same checks as
    filename_problems. They are offered as a correction and never block."""
    problems = []
    # Checked per segment: PROBLEM_CHARS includes '/', so the whole path
    # would be flagged at every separator.
    for seg in member.split("/"):
        for p in filename_problems(seg):
            problems.append("%r %s" % (seg, p))
    return problems


def suggest_member_path(member: str) -> str:
    """Return a suggested correction for a member path, each segment run
    through suggest_filename. Never applied automatically."""
    return "/".join(suggest_filename(seg) for seg in member.split("/"))


def is_reserved_member(member: str) -> bool:
    """True if any segment of a member path is the reserved record name
    (dataset.*.json)."""
    # Every segment, not only the last: a nested dataset.sub.json would
    # read as the record of a dataset called 'sub'.
    return any(is_reserved_name(seg) for seg in member.split("/"))


def record_filename(dataset: str) -> str:
    """Return the record filename for a dataset: dataset.<slug>.json."""
    # The slug always equals the prefix's last element, so the record key can be
    # built from the prefix alone, without listing.
    return "dataset.%s.json" % dataset


def _validate_parts(strand: str, project: str, sensitivity: str,
                    state: str, dataset: str) -> None:
    """Raise RedDataError for sensitivity 'red', ValueError for any other
    invalid path part."""
    # Red is refused here as well as in deposit.py, so no route can build
    # a key for it.
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
    """Return the dataset prefix
    {strand}/{project}/{sensitivity}/{state}/{dataset}, which is also the
    record's `identifier` field.

    Raises RedDataError for sensitivity 'red', ValueError for any other
    invalid part."""
    _validate_parts(strand, project, sensitivity, state, dataset)
    return "/".join((strand, project, sensitivity, state, dataset))


def record_key(strand: str, project: str, sensitivity: str,
               state: str, dataset: str) -> str:
    """Return the dataset record's object key: the prefix plus
    dataset.<slug>.json. Raises as dataset_prefix does."""
    return (dataset_prefix(strand, project, sensitivity, state, dataset)
            + "/" + record_filename(dataset))


def build_key(strand: str, project: str, sensitivity: str, state: str,
              dataset: str, member: str) -> str:
    """Return the object key
    {strand}/{project}/{sensitivity}/{state}/{dataset}/{member}.
    `member` may contain '/'.

    Raises RedDataError for sensitivity 'red'; ValueError for any other
    invalid part, an unusable member path, or a member that is the
    reserved record name (dataset.*.json)."""
    _validate_parts(strand, project, sensitivity, state, dataset)
    # Every caller passes through here, so this check is what keeps a
    # backslash out of every key.
    error = member_path_error(member)
    if error:
        raise ValueError(error)
    if is_reserved_member(member):
        raise ValueError(
            "%r matches dataset.*.json, which is reserved for dataset "
            "records" % member)
    return "/".join((strand, project, sensitivity, state, dataset, member))
