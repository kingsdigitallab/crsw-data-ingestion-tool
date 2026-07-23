"""Object key construction and filename checks for CRSW deposits.

Kept separate from sidecar.py deliberately: the path convention
(handbook v0.4 §4) may change independently of the metadata schema.
Object keys ALWAYS use '/' regardless of platform.
"""
import re
from typing import List

STRANDS = ("rs1", "rs2", "rs3", "rs4")
DOMAINS = ("quant", "geo", "pol", "narr", "parti")
STATES = ("0_raw", "1_interim", "2_final")
SENSITIVITIES = ("green", "amber")

PROBLEM_CHARS = '\\/:*?"<>|'

_PROJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class RedDataError(ValueError):
    """Raised when sensitivity 'red' reaches key construction. Red data
    never enters shared storage (handbook §3, §7.3)."""


def validate_project(project: str) -> bool:
    """Lowercase letters/digits, hyphen-separated. ASCII only."""
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


def build_key(strand: str, project: str, state: str, sensitivity: str,
              filename: str) -> str:
    """Build the object key {strand}/{project}/{state}/{sensitivity}/{filename}.

    Raises RedDataError for sensitivity 'red', ValueError for any other
    invalid part. This is a backstop — deposit.py refuses red earlier."""
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
    if not filename:
        raise ValueError("filename must not be empty")
    return "/".join((strand, project, state, sensitivity, filename))


def sidecar_key(data_key: str) -> str:
    """The sidecar key is always the data key + '.meta.json'."""
    return data_key + ".meta.json"
