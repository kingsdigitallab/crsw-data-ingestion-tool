#!/usr/bin/env python3
"""CRSW deposit tool - CLI front end.

ALL user interaction lives in this file: argparse, prompting, preview,
confirmation, progress, and translation of structured errors into
human-readable messages. The conventions live in the crsw_deposit
package (no interactive I/O) so the web deposit service imports the
same code; transfer.py is the rclone layer and stays CLI-only."""
import argparse
import collections
import glob as globlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from crsw_deposit import deposit_logic, keys, labels as labels_mod, noise, record, vocab
import transfer

RCLONE_DOWNLOAD_URL = "https://rclone.org/downloads/"

RCLONE_STANZA = """[ceph]
type = s3
provider = Ceph
access_key_id = YOUR_ACCESS_KEY
secret_access_key = YOUR_SECRET_KEY
endpoint = https://OBJECT-STORE-ENDPOINT-FROM-ERESEARCH
"""

# Every user-facing failure message lives here. Each message must say what
# happened, the most likely cause, and what to do next - for someone who
# has never seen the code. No socket errors, no S3 error codes, no stack
# traces.
MESSAGES = {
    "red_refused": (
        "This deposit is marked 'red'. Red data never enters shared storage -\n"
        "it belongs in the Trusted Research Environment (TRE). See the Data\n"
        "Governance Handbook, sections 3 and 7.3, or contact your domain steward."),
    "no_rclone": (
        "Could not find rclone.\n"
        "Looked on your PATH, then for ./rclone and ./rclone.exe next to\n"
        "your current folder and next to this script.\n"
        "Download it from %s and either install it or just drop the rclone\n"
        "binary in this folder - both work. On macOS/Linux it must be\n"
        "executable (chmod +x rclone); a browser download on macOS may\n"
        "also need 'xattr -d com.apple.quarantine rclone'."
        % RCLONE_DOWNLOAD_URL),
    "no_remote": (
        "rclone is installed but has no remote named '{remote}'.\n"
        "Run 'rclone config file' to find your config file and add this\n"
        "section (keys come from eResearch):\n\n" + RCLONE_STANZA + "\n"
        "Remotes that DO exist in your rclone config: {remotes}\n"
        "If one of those is the right one, run with --reconfigure to\n"
        "pick it from a menu and save it."),
    "unreachable": (
        "Cannot reach the storage endpoint - the KCL VPN is the usual cause.\n"
        "Check you are connected to it and try again. If the VPN is up and\n"
        "this persists, contact eResearch."),
    "credentials": (
        "The storage service rejected your credentials.\n"
        "If your access keys are new or were recently rotated, the config\n"
        "may be out of date - contact eResearch to confirm your keys."),
    "permission": (
        "You don't have write access to this location.\n"
        "Access is scoped by strand, so a credential for one strand cannot\n"
        "write to another - this is a permissions question, not a bug. If\n"
        "you believe you should have access here, contact eResearch."),
    "write_denied": (
        "You don't have write access to {target}.\n"
        "Access is scoped by strand, so a credential for one strand cannot\n"
        "write to another - this is a permissions question, not a bug. If\n"
        "you believe you should have write access here, re-run with\n"
        "--verbose and send the output to eResearch."),
    "not_found": (
        "The bucket was not found on the storage service.\n"
        "Deposits use the 'crsw' bucket by default. If you overrode it with\n"
        "--bucket or a saved config, check that value; if you are using the\n"
        "default, contact eResearch."),
    "unknown": (
        "The transfer failed for an unrecognised reason.\n"
        "Re-run with --verbose and send the output to eResearch support."),
    "verify_failed": (
        "The upload appeared to finish, but the stored object is missing or\n"
        "the wrong size. The deposit is NOT confirmed - re-run it. If this\n"
        "happens repeatedly, contact eResearch."),
    "unknown_subject": (
        "These terms are not in the subjects vocabulary: {terms}\n"
        "Unknown terms can't be accepted (they would silently fragment the\n"
        "catalogue). To propose an addition, open an issue on the vocabulary\n"
        "repository or ask your domain steward. The numbered listing above\n"
        "shows every valid term."),
    "reserved_name": (
        "{name} matches dataset.<name>.json, which is reserved for\n"
        "dataset records. Rename the file and re-run."),
    "bad_record": (
        "The dataset record at {key} exists but could not be used:\n"
        "{reason}\n"
        "Nothing was uploaded or overwritten. Inspect it with\n"
        "  rclone cat {key}\n"
        "or restore an earlier version (bucket versioning keeps history),\n"
        "then re-run."),
    "record_invalid": (
        "The assembled dataset record failed its own validation, so it was\n"
        "NOT written. Files that already uploaded are fine and a re-run is\n"
        "safe. This is a tool problem, not yours: re-run with --verbose and\n"
        "send the output to eResearch."),
    "restricted_first": (
        "Nothing deposited: access restrictions must be in place BEFORE the\n"
        "first deposit, not applied afterwards. Depositing first and\n"
        "restricting later leaves a window where the data is readable by\n"
        "everyone with strand access. Contact eResearch or your data\n"
        "steward to set up the restriction, then re-run."),
}


# ----------------------------------------------------------------- colour

_STYLE_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32",
                "yellow": "33", "cyan": "36"}


def enable_vt() -> None:
    """Enable ANSI escape processing on Windows 10+ consoles.
    Harmless no-op elsewhere or on any failure (r2 §4)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def colour_enabled() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if "FORCE_COLOR" in os.environ:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def interactive() -> bool:
    """True when there is a human at the other end of stdin. Used to gate
    prompts that are not already covered by --dry-run (offer_to_save_settings)
    - every existing tty check (colour_enabled, the progress-bar check)
    reads stdout, which is the wrong stream for deciding whether a
    prompt is safe to show."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def style(text: str, *names: str) -> str:
    """The single home for ANSI escapes. Plain text when colour is off.
    Colour must never be the only signal - the words carry the meaning."""
    if not names or not colour_enabled():
        return text
    codes = ";".join(_STYLE_CODES[n] for n in names)
    return "\x1b[%sm%s\x1b[0m" % (codes, text)


# ---------------------------------------------------------------- helpers

def say(text: str) -> None:
    print(text)


def warn(text: str) -> None:
    print(style("WARNING: " + text, "yellow"), file=sys.stderr)


def fail(text: str, code: int = 1) -> int:
    print("\n" + style(text, "red"), file=sys.stderr)
    return code


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = " [%s]: " % default if default else ": "
    while True:
        answer = input(prompt + suffix).strip()
        if answer:
            return answer
        if default is not None:
            return default


def ask_choice(prompt: str, choices) -> str:
    choice_str = "/".join(choices)
    while True:
        answer = input("%s (%s): " % (prompt, choice_str)).strip().lower()
        if answer in choices:
            return answer
        say("Please enter one of: %s" % choice_str)


def resolve_choice(raw, entries):
    """Match user input against (number, value, hint) entries.
    Number or literal value, case-insensitive. None if no match."""
    token = (raw or "").strip().lower()
    if not token:
        return None
    for number, value, _hint in entries:
        if token == number.lower() or token == value.lower():
            return value
    return None


def choice_lines(title, entries):
    lines = [style(title + ":", "bold")]
    if all(not hint for _n, _v, hint in entries):
        lines.append("  " + "    ".join(
            "%s %s" % (style("[%s]" % n, "dim"), v) for n, v, _ in entries))
    else:
        width = max(len(v) for _n, v, _h in entries)
        for n, v, hint in entries:
            lines.append("  %s %-*s   %s" % (style("[%s]" % n, "dim"),
                                             width, v, hint))
    return lines


def ask_select(title, entries):
    """Numbered select (r2 §1). Never silently defaults: invalid input
    re-prompts with the list redisplayed."""
    while True:
        for line in choice_lines(title, entries):
            say(line)
        value = resolve_choice(input("> "), entries)
        if value is not None:
            return value
        say("Enter a number or value from the list.")


STRAND_ENTRIES = [(str(i), s, "") for i, s in enumerate(keys.STRANDS, 1)]
STATE_ENTRIES = [(v.split("_")[0], v, h) for v, h in
                 zip(keys.STATES, ("raw, as received", "in progress",
                                   "released or shared"))]
SENSITIVITY_ENTRIES = [("1", "green", "publicly shareable"),
                       ("2", "amber", "internal, strand-scoped")]
SOURCE_TYPE_ENTRIES = [
    ("1", "archive", "existing collection or repository"),
    ("2", "survey", "primary data collection instrument"),
    ("3", "scrape", "automated extraction from an online source"),
    ("4", "instrument", "sensor, satellite, or other device output"),
    ("5", "partner", "supplied by a partner organisation"),
    ("6", "derived", "produced from other data already held"),
    ("7", "other", "none of the above"),
]


def subject_entries(vocab_dict):
    """(number, term, facet) numbered continuously across facets in
    vocabulary-file order, so numbers match the displayed listing."""
    entries = []
    n = 0
    for facet, terms in vocab_dict.get("facets", {}).items():
        for term in terms:
            n += 1
            entries.append((str(n), term, facet))
    return entries


def subject_depths(vocab_dict):
    """term -> depth under its broader term (r9 §1.8). Every depth is 0
    for a flat vocabulary, so the listing prints exactly as before."""
    from crsw_deposit import authority
    depths = {}
    for facet in vocab.facets(vocab_dict):
        for term, depth in authority.tree(vocab_dict, facet):
            depths[term] = depth
    return depths


def subject_listing_lines(entries, width, depths=None):
    """Grouped-by-facet listing. Two columns at >=100 chars (r2 §4).
    With `depths`, a term sits indented under its broader term."""
    depths = depths or {}
    columns = 2 if width >= 100 else 1
    num_w = max(len(n) for n, _t, _f in entries)
    term_w = max(len(t) + 2 * depths.get(t, 0) for _n, t, _f in entries)
    lines = []
    facet = None
    row = []
    for n, term, f in entries:
        if f != facet:
            if row:
                lines.append("    " + "".join(row))
                row = []
            facet = f
            lines.append("")
            lines.append("  " + style(facet, "bold"))
        shown = "  " * depths.get(term, 0) + term
        cell = "%s %-*s  " % (style("[%*s]" % (num_w, n), "dim"), term_w, shown)
        row.append(cell)
        if len(row) == columns:
            lines.append("    " + "".join(row))
            row = []
    if row:
        lines.append("    " + "".join(row))
    return lines


def resolve_subjects(raw, entries):
    """Resolve comma-separated numbers/terms (r2 §4). Returns
    (resolved deduped in order, unknown tokens). Unknown tokens are
    named, never silently dropped."""
    by_number = {n: t for n, t, _f in entries}
    by_term = {t.lower(): t for _n, t, _f in entries}
    resolved = []
    unknown = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        term = by_number.get(token) or by_term.get(token.lower())
        if term is None:
            unknown.append(token)
        elif term not in resolved:
            resolved.append(term)
    return resolved, unknown


def ask_yes_no(prompt: str, default_no: bool = True) -> bool:
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value = value / 1024
    return "%d B" % n


# A "source" is one local file resolved from the command line: `path` is
# the local Path exactly as the OS gave it (never rewritten), `member` is
# the validated, NFC-normalised string that becomes both the object key's
# last element and the manifest `path` field (r7 §1). For an explicit
# file argument member is just the basename, unchanged from before r7;
# for a file found inside a folder argument it is the path relative to
# that argument's root, so structure is preserved instead of flattened.
Source = collections.namedtuple("Source", ("path", "member"))

# OS/editor noise that would otherwise silently double the manifest when
# a whole folder is walked (r7 §2) - excluded by default, never silently:
# resolve_files always reports how many it dropped, and --include-noise
# keeps them. The rules live in crsw_deposit.noise so the web route
# applies exactly the same set.
NOISE_DIR_NAMES = noise.NOISE_DIR_NAMES
_is_noise_file = noise.is_noise_file


def _member_for(path: Path, root: Path) -> str:
    """path's member string relative to root (r7 §1's anchoring rule),
    NFC-normalised so the same file deposited from Windows/Linux and
    from macOS (which hands back decomposed Unicode) produces the same
    manifest path (r7 §4)."""
    rel = os.path.relpath(str(path), str(root))
    return keys.normalise_member_path(Path(rel).parts)


def _pattern_root(pattern: str) -> Path:
    """The literal directory prefix before a glob pattern's first
    wildcard component - the anchor every match under that pattern is
    made relative to (r7 §1), e.g. 'data/*/results.csv' -> 'data'.

    Uses Path(pattern).parts rather than a manual separator split: a
    plain string split-and-rejoin of an absolute Windows path silently
    turns 'C:\\Users\\...' into the drive-RELATIVE 'C:Users\\...' (a
    different path, resolved against the drive's current directory, not
    the root) - pathlib's own parsing keeps the drive anchor intact."""
    parts = Path(pattern).parts
    literal = []
    for part in parts:
        if globlib.has_magic(part):
            break
        literal.append(part)
    return Path(*literal) if literal else Path(".")


def _walk_dir(dirpath: Path, root: Path, include_noise: bool,
             sources: List[Source], problems: List[str], notes: List[str],
             stats: Dict, seen: Dict) -> None:
    """Walk dirpath (a matched directory) and add one Source per file
    found, with member paths relative to `root` - which is dirpath
    itself for a bare folder argument, but the shared pattern root for a
    directory matched by a wildcard (r7 §1), so a whole tree's contents
    land under one consistent anchor either way.

    os.walk, not Path.rglob (r7 §1): rglob swallows OSError on an
    unreadable subdirectory (a silent omission), can't be pruned before
    descending, yields filesystem order (making the manifest differ
    between NTFS/APFS/ext4), and follows symlinked directories."""
    def onerror(err):
        problems.append("cannot read %s: %s"
                        % (err.filename, err.strerror or err))

    for here, dirnames, filenames in os.walk(str(dirpath), onerror=onerror,
                                             followlinks=False):
        dirnames.sort()
        filenames.sort()
        keep = []
        for d in dirnames:
            full = Path(here) / d
            if not include_noise and d in NOISE_DIR_NAMES:
                stats["noise"] += 1
                continue
            if full.is_symlink():
                stats["symlinked_dirs"].append(str(full))
                continue
            keep.append(d)
        dirnames[:] = keep
        if not dirnames and not filenames:
            stats["empty_dirs"] += 1
        for name in filenames:
            full = Path(here) / name
            if full.is_symlink():
                if not full.exists():
                    problems.append("cannot read %s: broken link" % full)
                    continue
                stats["symlinks"] += 1
            if not include_noise and _is_noise_file(name):
                stats["noise"] += 1
                continue
            if name.startswith("."):
                stats["hidden"] += 1
            try:
                if full.stat().st_size == 0:
                    stats["zero_byte"] += 1
            except OSError:
                pass
            try:
                resolved = str(full.resolve())
            except OSError:
                resolved = str(full)
            if resolved in seen:
                notes.append("%s already included (as %s) - skipping "
                             "duplicate" % (full, seen[resolved]))
                continue
            member = _member_for(full, root)
            seen[resolved] = member
            sources.append(Source(full, member))


def resolve_files(patterns: List[str],
                  include_noise: bool = False
                  ) -> Tuple[List[Source], List[str], List[str]]:
    """Expand globs and walk folders (r7 §1 - "no --recursive yet" is now
    built). Returns (sources, problems, notes): problems are fatal-ish
    (no match, unreadable), notes explain what the tool did on your
    behalf (folder roots, exclusions) - said, never silent."""
    sources: List[Source] = []
    problems: List[str] = []
    notes: List[str] = []
    stats = {"noise": 0, "hidden": 0, "symlinks": 0, "symlinked_dirs": [],
             "empty_dirs": 0, "zero_byte": 0}
    seen: Dict[str, str] = {}

    for pattern in patterns:
        matches = globlib.glob(pattern, recursive=True)
        literal = False
        if not matches:
            # A real file named e.g. 'data[1].csv' is otherwise reported
            # as "no file matches" because glob reads '[1]' as a
            # character class, not a literal (r7 §1).
            if os.path.lexists(pattern):
                matches = [pattern]
                literal = True
            else:
                problems.append("no file matches %r" % pattern)
                continue

        bare = literal or not globlib.has_magic(pattern)
        if bare:
            p = Path(matches[0])
            root = p if p.is_dir() else p.parent
        else:
            root = _pattern_root(pattern)
        if literal:
            notes.append("taking %r literally; the brackets would "
                         "otherwise be read as a wildcard" % pattern)

        for m in matches:
            p = Path(m)
            if p.is_dir():
                before = len(sources)
                _walk_dir(p, root, include_noise, sources, problems, notes,
                          stats, seen)
                if bare:
                    notes.append(
                        "%s -> member paths relative to %s (%d file(s))"
                        % (pattern, root, len(sources) - before))
            elif p.is_file():
                resolved = str(p.resolve())
                member = _member_for(p, root)
                if resolved in seen:
                    notes.append("%s already included (as %s) - skipping "
                                 "duplicate" % (p, seen[resolved]))
                    continue
                seen[resolved] = member
                sources.append(Source(p, member))
            else:
                problems.append("cannot read %s" % p)

    if stats["noise"]:
        notes.append("Excluded %d OS metadata/noise file(s) or folder(s) "
                     "(--include-noise to keep them)." % stats["noise"])
    if stats["hidden"]:
        notes.append("%d hidden/dotfile(s) included." % stats["hidden"])
    if stats["symlinks"]:
        notes.append("Followed %d symlinked file(s)." % stats["symlinks"])
    if stats["symlinked_dirs"]:
        shown = stats["symlinked_dirs"][:5]
        extra = len(stats["symlinked_dirs"]) - len(shown)
        notes.append(
            "Did not descend %d symlinked folder(s): %s%s - deposit them "
            "directly if you mean to include their contents."
            % (len(stats["symlinked_dirs"]), ", ".join(shown),
               (" and %d more" % extra) if extra else ""))
    if stats["empty_dirs"]:
        notes.append("%d empty folder(s) contribute nothing (object "
                     "storage has no directories)." % stats["empty_dirs"])
    if stats["zero_byte"]:
        notes.append("%d file(s) are zero bytes." % stats["zero_byte"])
    return sources, problems, notes


def set_member(plan: Dict, meta: Dict, new_member: str) -> None:
    """Rewrite a plan's key and member TOGETHER - they must never drift
    apart. The completion check at the end of a deposit reconstructs
    every member's key as prefix + '/' + entry['path'], so a plan whose
    key and member disagree would fail verification only after every
    file has uploaded and the record has been written - the worst
    possible point for that bug to surface."""
    plan["member"] = new_member
    plan["key"] = keys.build_key(meta["strand"], meta["project"],
                                 meta["sensitivity"], meta["state"],
                                 meta["dataset"], new_member)


def plan_deposits(sources: List[Source], meta: Dict,
                  per_file: Dict) -> List[Dict]:
    """Pure planning: one dict per source with its object key and
    manifest overrides. per_file maps member -> coverage overrides
    (r5 Q5). Raises ValueError when two sources would land at the same
    key - that would be a silent overwrite inside one batch."""
    planned = deposit_logic.plan_keys(meta, [s.member for s in sources],
                                      display=[str(s.path) for s in sources])
    return [{"path": src.path, "member": member, "key": key,
             "entry_overrides": dict(per_file.get(member, {}))}
            for src, (member, key) in zip(sources, planned)]


def prepare_entries(plans: List[Dict]) -> List[Dict]:
    """Checksum and stat every file into its manifest entry (r5 §2),
    aligned with `plans` by index. Reads local files; never uploads."""
    entries = []
    for plan in plans:
        member = plan["member"]
        overrides = plan.get("entry_overrides", {})
        entries.append(record.manifest_entry(
            member,
            record.sha256_file(plan["path"]),
            plan["path"].stat().st_size,
            temporal=(record.temporal_object(overrides.get("coverage_start"),
                                             overrides.get("coverage_end"))
                      if overrides.get("coverage_start")
                      or overrides.get("coverage_end") else None),
            fmt=record.guess_format(member)))
    return entries


def classify_members(entries: List[Dict], existing: Optional[Dict]):
    """(added, updated, unchanged) paths for the batch against the
    existing record, if any (r5 Q4). Drives the preview and the report."""
    existing_files = existing.get("files") if existing else None
    _, added, updated = record.merge_manifest(existing_files, entries)
    unchanged = sorted(record.unchanged_paths(existing_files, entries))
    return added, updated, unchanged


def duplicate_content_warnings(entries: List[Dict],
                               existing: Optional[Dict]) -> List[str]:
    """A new member whose checksum matches an EXISTING member at a
    different path is still added, not merged - deposits are additive
    and the tool never removes a manifest member (r7 §1). The likely
    cause is a file previously deposited flat and now re-deposited from
    inside a folder, which silently doubles the bytes in the dataset if
    nobody is told. Warn before the point of no return; never block -
    two genuine copies in two folders is legal."""
    existing_files = (existing or {}).get("files") or []
    by_checksum: Dict[str, List[str]] = {}
    for e in existing_files:
        by_checksum.setdefault(e.get("checksum_sha256"), []).append(e["path"])
    warnings = []
    for entry in entries:
        others = [p for p in by_checksum.get(entry["checksum_sha256"], [])
                 if p != entry["path"]]
        if others:
            warnings.append(
                "%r has the same content as %s, already in this dataset - "
                "depositing adds a second copy (the tool never removes "
                "members)." % (entry["path"], ", ".join(repr(p) for p in others)))
    return warnings


def preview_lines(plans: List[Dict], meta: Dict, existing: Optional[Dict],
                  classification, limit: int = 3) -> List[str]:
    """Dataset-first preview: the record, the member classification, then
    up to `limit` member keys. Overwrites are stated here - there is no
    separate collision prompt; the point-of-no-return confirmation covers
    the whole plan (r5 Q4)."""
    added, updated, unchanged = classification
    prefix = keys.dataset_prefix(meta["strand"], meta["project"],
                                 meta["sensitivity"], meta["state"],
                                 meta["dataset"])
    lines = ["  dataset %s (version %s)"
             % (style(prefix, "cyan"), meta.get("version"))]
    record_key = prefix + "/" + keys.record_filename(meta["dataset"])
    if existing:
        before = len(existing.get("files", []))
        lines.append("    record: %s  (updates existing, %d -> %d files)"
                     % (record_key, before, before + len(added)))
    else:
        lines.append("    record: %s  (first deposit)" % record_key)
    lines.append("    members: %d added, %d updated, %d unchanged"
                 % (len(added), len(updated), len(unchanged)))
    for plan in plans[:limit]:
        name = plan["member"]
        note = ""
        if name in updated:
            note = "  (new version; previous kept by bucket versioning)"
        elif name in unchanged:
            note = "  (unchanged - upload will be skipped)"
        lines.append("      %s%s" % (plan["key"], note))
    if len(plans) > limit:
        lines.append("      ... and %d more" % (len(plans) - limit))
    lines.append("    coverage %s to %s | subjects: %s" % (
        meta.get("coverage_start"), meta.get("coverage_end"),
        ", ".join(meta.get("subject", []))))
    refs = meta.get("derived_from") or []
    if refs:
        names = [r.get("identifier") or r.get("url") or r.get("citation") or "?"
                 for r in refs]
        more = "" if len(names) <= 2 else ", ... and %d more" % (len(names) - 2)
        lines.append("    derived from: %d reference(s): %s%s"
                     % (len(refs), ", ".join(names[:2]), more))
    acts = meta.get("provenance") or []
    if acts:
        tools = sorted({(a.get("tool") or {}).get("name") for a in acts
                        if (a.get("tool") or {}).get("name")})
        lines.append("    provenance: %d activit%s%s" % (
            len(acts), "y" if len(acts) == 1 else "ies",
            (", tool " + ", ".join(tools)) if tools else ""))
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deposit.py",
        description="Deposit research files into CRSW shared storage, "
                    "described by one dataset record per prefix.")
    p.add_argument("files", nargs="+", metavar="FILE_OR_GLOB")
    p.add_argument("--include-noise", action="store_true",
                   help="deposit OS/editor noise files (.DS_Store, "
                        "Thumbs.db, ._* etc.) instead of excluding them")
    p.add_argument("--strand", choices=keys.STRANDS)
    p.add_argument("--project")
    p.add_argument("--dataset", help="dataset name within the project "
                   "(r6: a project may hold several datasets)")
    p.add_argument("--domain", help="data domain code (see vocabulary)")
    p.add_argument("--state", choices=keys.STATES)
    p.add_argument("--sensitivity")  # validated by hand so 'red' gets OUR message
    p.add_argument("--dry-run", action="store_true",
                   help="preview keys and the dataset record, upload nothing")
    p.add_argument("--provenance", metavar="FILE",
                   help="JSON file of provenance activities (and optionally "
                        "derived_from references) to record; skips the two "
                        "origin questions")
    p.add_argument("--remote", default=None,
                   help="rclone remote name (default: ceph, or your saved config)")
    p.add_argument("--bucket", default=None,
                   help="target bucket (default: crsw, or your saved config)")
    p.add_argument("--reconfigure", action="store_true",
                   help="re-run remote/bucket setup")
    p.add_argument("--verbose", action="store_true",
                   help="show raw rclone output on errors")
    return p


# ----------------------------------------------------------------- config

def config_path() -> Path:
    if os.name == "nt":
        return vocab.cache_dir() / "config.json"
    return Path.home() / ".config" / "crsw-deposit" / "config.json"


def load_config(path: Optional[Path] = None) -> dict:
    path = path if path is not None else config_path()
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict, path: Optional[Path] = None) -> None:
    path = Path(path) if path is not None else config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": Path.write_text applies platform newline
        # translation (CRLF on Windows), which is harmless for a config
        # file nobody hashes but inconsistent with every other file this
        # tool writes - kept explicit here too (r7 §4).
        with open(str(path), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(cfg, indent=2) + "\n")
    except OSError:
        warn("could not save settings to %s - flags still work" % path)


def resolve_settings(args, cfg) -> Tuple[str, str]:
    """Precedence: command-line flag -> config file -> built-in default."""
    remote = args.remote or cfg.get("remote") or "ceph"
    bucket = args.bucket or cfg.get("bucket") or "crsw"
    return remote, bucket


def setting_sources(args, cfg) -> Tuple[str, str]:
    """Where each resolved setting came from, for the Target line. Must
    be called BEFORE resolve_settings overwrites args.remote/args.bucket."""
    remote_src = ("--remote flag" if args.remote
                  else "saved config" if cfg.get("remote") else "default")
    bucket_src = ("--bucket flag" if args.bucket
                  else "saved config" if cfg.get("bucket") else "default")
    return remote_src, bucket_src


def first_run_setup(rclone) -> dict:
    say(style("First-run setup: which rclone remote and bucket should "
              "deposits go to?", "bold"))
    try:
        names = transfer.remote_names(rclone)
    except transfer.TransferError:
        names = []
    if names:
        remote = ask_select("Remote",
                            [(str(i), n, "") for i, n in enumerate(names, 1)])
    else:
        remote = ask("Remote name")
    bucket = ask("Bucket", default="crsw")
    cfg = {"remote": remote, "bucket": bucket}
    save_config(cfg)
    say("Saved to %s (re-run setup any time with --reconfigure)." % config_path())
    return cfg


def offer_to_save_settings(flagged_remote: Optional[str],
                           flagged_bucket: Optional[str], cfg: dict,
                           remote: str, bucket: str, already_set_up: bool,
                           dry_run: bool) -> None:
    """--remote/--bucket are transient by default; offer once to persist
    them to config.json when a flag actually changed the resolved value.
    Stays silent (no prompt at all) when: first_run_setup just ran or
    --reconfigure did (already_set_up - no double prompt), neither flag
    was passed, --dry-run (the non-interactive support-reproduction
    path must never mutate the machine), stdin isn't a tty (scripted
    runs), or the proposed values already match what's saved."""
    if already_set_up or dry_run or not interactive():
        return
    if flagged_remote is None and flagged_bucket is None:
        return
    proposed = dict(cfg)
    proposed["remote"] = remote
    proposed["bucket"] = bucket
    if proposed.get("remote") == cfg.get("remote") and \
            proposed.get("bucket") == cfg.get("bucket"):
        return
    if ask_yes_no("Save %s:%s as your default target?" % (remote, bucket)):
        save_config(proposed)
        say("Saved to %s." % config_path())
    else:
        say("Not saved - the flags applied to this run only.")


# ---------------------------------------------------------------- prompts

def resolve_project_choice(raw, entries):
    """'new' for n/new, else resolve_choice(raw, entries).
    A project or dataset literally named 'n' or 'new' stays reachable
    by number. Generic across both (r6 §1: the dataset picker reuses
    this unchanged, no second implementation)."""
    token = (raw or "").strip().lower()
    if token in ("n", "new"):
        return "new"
    return resolve_choice(raw, entries)


def check_restricted_access(kind: str) -> None:
    """Matrix-first (permissions discussion, r6 §8): if a newly created
    project or dataset will need access restricted beyond the strand,
    that restriction must be set up before the first deposit - never
    deposit then restrict, which leaves a window where the data is
    readable by everyone with strand access. Raises ValueError to abort
    if the answer is yes. Only asked on a genuine creation confirmation,
    never on the honest "use this, listing failed" fallback."""
    if ask_yes_no("Will access to this %s need restricting beyond the "
                  "strand?" % kind, default_no=True):
        raise ValueError(MESSAGES["restricted_first"])


def prompt_new_project(container: str, existing: List[str],
                       confirm_create: bool = True, kind: str = "project") -> str:
    """Ask for a project or dataset name, normalise it, warn on
    near-matches, and confirm. `container` describes where it lives
    (a strand for a project, a full state prefix for a dataset - r6
    §1's picker is this same function one level down).
    confirm_create=False softens the wording to "Use X" for when the
    listing failed and we can't actually claim it's new."""
    while True:
        raw = ask("New %s name (lowercase, hyphens, no spaces)" % kind)
        name = keys.normalise_project(raw)
        if not name:
            say("Nothing usable remains after normalising %r - %s names "
                "are lowercase letters/digits with hyphens (e.g. csac, "
                "treaty-texts)." % (raw, kind))
            continue
        if name != raw:
            say("  -> normalised to: %s" % name)
        if name in existing:
            # Exact match only: a legacy 'CSAC' is NOT the same prefix as
            # 'csac' and must go through the near-match warning instead.
            say("'%s' already exists in %s - using it." % (name, container))
            return name
        similar = keys.similar_projects(name, existing)
        if similar:
            warn("similar to existing '%s' - sure this is different?"
                 % "', '".join(similar))
        if confirm_create:
            confirmed = ask_yes_no("Create new %s '%s' in %s?"
                                   % (kind, name, container))
        else:
            confirmed = ask_yes_no("Use %s '%s' in %s?"
                                   % (kind, name, container))
        if confirmed:
            if confirm_create:
                check_restricted_access(kind)
            return name


def prompt_project(container: str, listing: Optional[List[str]],
                   kind: str = "project") -> str:
    """Numbered picker over existing project (or dataset) names (r4 §2,
    generalised one level down for datasets by r6 §1), with [n] for a
    new one. listing is None when it could not be fetched - never
    presented as empty (it may be a permissions artefact)."""
    plural = kind + "s"
    if listing is None:
        say("Couldn't list existing %s in %s - you can still enter one."
            % (plural, container))
        return prompt_new_project(container, [], confirm_create=False,
                                  kind=kind)
    if not listing:
        say("No %s in %s yet - creating the first." % (plural, container))
        return prompt_new_project(container, [], kind=kind)
    entries = [(str(i), p, "") for i, p in enumerate(listing, 1)]
    while True:
        for line in choice_lines(
                "Existing %s in %s" % (plural.capitalize(), container),
                entries):
            say(line)
        say("  %s new %s" % (style("[n]", "dim"), kind))
        choice = resolve_project_choice(input("> "), entries)
        if choice == "new":
            return prompt_new_project(container, listing, kind=kind)
        if choice is not None:
            if not keys.validate_project(choice):
                say("'%s' predates the naming rule and can't be used as-is "
                    "(try '%s' as a new %s)."
                    % (choice, keys.normalise_project(choice), kind))
                continue
            return choice
        say("Enter a number, a %s name, or 'n' for a new %s."
            % (kind, kind))


def check_project_flag(project: str, container: str,
                       listing: Optional[List[str]], dry_run: bool,
                       kind: str = "project") -> None:
    """--project (or --dataset) skips the picker, but a name that
    doesn't exist yet is a creation: confirm once (r4 §2). Never prompts
    under --dry-run - that's the non-interactive reproduction path.
    Raises ValueError if the user declines."""
    if listing is None or project in listing:
        return
    similar = keys.similar_projects(project, listing)
    if dry_run:
        say("note: %s '%s' does not exist in %s yet - a real run will "
            "ask to confirm creating it." % (kind, project, container))
        if similar:
            say("note: '%s' is similar to existing '%s'."
                % (project, "', '".join(similar)))
        return
    if similar:
        warn("similar to existing '%s' - sure this is different?"
             % "', '".join(similar))
    if not ask_yes_no("%s '%s' does not exist in %s. Create it as a "
                      "new %s?" % (kind.capitalize(), project, container, kind)):
        raise ValueError(
            "nothing deposited: %s '%s' was not confirmed - pick an "
            "existing %s or re-run and confirm" % (kind, project, kind))
    check_restricted_access(kind)


def prompt_origin(meta: Dict, vocab_dict: Dict) -> None:
    """The two origin questions of a first deposit (r8 §3), both
    skippable. What this came from, one identifier or URL per line;
    then, if a script or notebook made it, the tool and the kind of
    step, as one provenance activity. Re-deposits do not ask again: a
    further activity is `--provenance FILE`."""
    say("What was this derived from? Dataset identifier "
        "(strand/project/sensitivity/state/dataset) or URL, one per "
        "line; blank line to finish (Enter to skip).")
    lines = []
    while True:
        line = input("> ").strip()
        if not line:
            break
        lines.append(line)
    refs = record.references_from_lines(lines)
    if refs:
        meta["derived_from"] = refs
        say("Derived from: %s" % ", ".join(
            r.get("identifier") or r.get("url") or r.get("citation")
            for r in refs))
    elif meta.get("source_type") == "derived":
        warn("source type is 'derived' but nothing was named - the record "
             "will carry a warning until a reference is added.")

    if not ask_yes_no("Was this dataset produced by a script or notebook "
                      "you can name?", default_no=True):
        return
    entries = [(str(i), a["code"], a.get("label", ""))
               for i, a in enumerate(vocab.activities(vocab_dict), 1)]
    activity = {"activity": ask_select("Kind of step", entries)}
    tool = {"name": ask("Tool or script name")}
    repo = input("Repository URL (Enter to skip): ").strip()
    if repo:
        tool["repo"] = repo
    commit = input("Commit hash (Enter to skip): ").strip()
    if commit:
        tool["commit"] = commit
    activity["tool"] = tool
    description = input("One or two sentences on what the step did "
                        "(Enter to skip): ").strip()
    if description:
        activity["description"] = description
    meta["provenance"] = [activity]


def prompt_metadata(args, vocab_dict: Dict, list_dirs=None,
                    probe_write=None, fetch_record=None):
    """Collect batch metadata, honouring flags. Returns (meta, existing)
    where existing is the parsed dataset record already at the target
    prefix, or None for a first deposit.

    list_dirs is a callable prefix -> Optional[List[str]] (None when the
    remote isn't usable), used for BOTH the project picker (keyed on the
    strand) and the dataset picker one level down (keyed on the full
    state prefix - r6 §1, same picker mechanism reused, not a second
    implementation). Each is called once per invocation.

    probe_write is a callable prefix -> Optional[error kind] (None when
    the remote isn't usable or under --dry-run). It runs as soon as the
    full deposit prefix is known, so a write denial surfaces four prompts
    in rather than after the whole interview (spec §8: fail early).
    Raises transfer.TransferError with the probed prefix in .detail.

    fetch_record is a callable key -> (text, err) per transfer.read_key.
    An existing record answers most of the interview (r5 §4): repeat
    deposits stop re-asking what the dataset already knows."""
    meta = {}

    meta["strand"] = args.strand or ask_select("Strand", STRAND_ENTRIES)

    if args.sensitivity:
        sensitivity = args.sensitivity
        if sensitivity == "red":
            raise keys.RedDataError()
        if sensitivity not in keys.SENSITIVITIES:
            raise ValueError("sensitivity must be green or amber, got %r"
                             % sensitivity)
    else:
        while True:
            for line in choice_lines("Sensitivity", SENSITIVITY_ENTRIES):
                say(line)
            raw = input("> ").strip().lower()
            if raw == "red":
                raise keys.RedDataError()
            sensitivity = resolve_choice(raw, SENSITIVITY_ENTRIES)
            if sensitivity is not None:
                break
            say("Enter a number or value from the list.")
    meta["sensitivity"] = sensitivity

    listing = list_dirs(meta["strand"]) if list_dirs else None
    if args.project:
        project = args.project
        if not keys.validate_project(project):
            raise ValueError(
                "project %r: use lowercase letters/digits and hyphens "
                "(e.g. csac, treaty-texts)" % project)
        check_project_flag(project, meta["strand"], listing, args.dry_run)
    else:
        project = prompt_project(meta["strand"], listing)
    meta["project"] = project

    meta["state"] = args.state or ask_select("State", STATE_ENTRIES)

    # r6 §1: the dataset is a path element; a project may hold several.
    # Mirrors the project picker exactly, one level down, keyed on the
    # full state prefix rather than the strand - same functions, a
    # parameterised call (kind="dataset"), not a second implementation.
    container = "/".join((meta["strand"], meta["project"],
                          meta["sensitivity"], meta["state"]))
    dataset_listing = list_dirs(container) if list_dirs else None
    if args.dataset:
        if not keys.validate_project(args.dataset):
            raise ValueError(
                "dataset %r: use lowercase letters/digits and hyphens "
                "(e.g. sentinel2-imagery)" % args.dataset)
        check_project_flag(args.dataset, container, dataset_listing,
                           args.dry_run, kind="dataset")
        meta["dataset"] = args.dataset
    else:
        meta["dataset"] = prompt_project(container, dataset_listing,
                                         kind="dataset")

    prefix = container + "/" + meta["dataset"]
    if probe_write:
        kind = probe_write(prefix)
        if kind:
            raise transfer.TransferError(kind, prefix)

    # r5 §4: an existing record answers instead of re-asking. A record
    # that exists but cannot be read is fatal on a real run - building on
    # it unread would silently drop members (dry-run warns and previews
    # as a first deposit; an unparseable record is surfaced either way).
    existing = None
    record_key = prefix + "/" + keys.record_filename(meta["dataset"])
    if fetch_record:
        text, err = fetch_record(record_key)
        if text is not None:
            try:
                existing = record.parse_record(text)
            except record.RecordParseError as e:
                e.key = record_key
                raise
        elif err != "absent":
            if args.dry_run:
                warn("could not read the existing dataset record (%s) - "
                     "previewing as a first deposit." % err)
            else:
                raise transfer.TransferError(err, record_key)
    if existing:
        # r6 §2: the record must belong where it sits - a dataset or
        # identifier that disagrees with the location means the record
        # was moved or hand-edited. Error, not warning.
        if (existing.get("dataset") != meta["dataset"]
                or existing.get("identifier") != prefix):
            e = record.RecordParseError(
                "the record's dataset/identifier (%r / %r) do not match "
                "its location %s"
                % (existing.get("dataset"), existing.get("identifier"),
                   prefix))
            e.key = record_key
            raise e
        say("")
        say("Existing dataset found: %s %s - %d files, modified %s."
            % (meta["dataset"], existing.get("version", "?"),
               len(existing.get("files", [])),
               (existing.get("modified") or "?")[:10]))
        say("Adding to it. Current values will be kept unless you "
            "change them.")

    domain_entries = vocab.domains(vocab_dict)
    codes = [d["code"] for d in domain_entries]
    if args.domain:
        if args.domain not in codes:
            raise ValueError("domain %r is not one of: %s"
                             % (args.domain, ", ".join(codes)))
        if (existing and existing.get("domain")
                and existing["domain"] != args.domain):
            warn("--domain %s differs from the existing record's %s - "
                 "using %s." % (args.domain, existing["domain"], args.domain))
        meta["domain"] = args.domain
    elif existing and existing.get("domain") in codes:
        meta["domain"] = existing["domain"]
    else:
        meta["domain"] = ask_select(
            "Domain", [(str(i), d["code"], d["label"])
                       for i, d in enumerate(domain_entries, 1)])
    if existing and existing.get("steward"):
        meta["steward"] = existing["steward"]
    else:
        chosen = next(d for d in domain_entries if d["code"] == meta["domain"])
        if chosen["steward"] and chosen["steward"] != "TBC":
            meta["steward"] = chosen["steward"]
            say("Steward: %s (from domain %s)"
                % (chosen["steward"], meta["domain"]))

    default_version = (existing.get("version") if existing else None) or "1-0"
    while True:
        version = record.normalise_version(ask("Version",
                                               default=default_version))
        if version is not None:
            meta["version"] = version
            break
        say("Version must be two integers like 3-0 (or 3.0 / v3-0, "
            "which I will normalise).")

    existing_temporal = (existing.get("temporal") or {}) if existing else {}
    for label, field, temporal_part in (
            ("Coverage start (year or YYYY-MM-DD)", "coverage_start", "start"),
            ("Coverage end (year or YYYY-MM-DD)", "coverage_end", "end")):
        default = existing_temporal.get(temporal_part)
        while True:
            value = ask(label, default=default)
            err = record.coverage_error(value)
            if err:
                say(err)
                continue
            meta[field] = value
            break

    existing_subjects = list(existing.get("subject") or []) if existing else []
    if existing_subjects and not record.unknown_subjects(
            existing_subjects, vocab.all_terms(vocab_dict)):
        meta["subject"] = existing_subjects
    else:
        # r9 §1.10: stale terms are mapped through the vocabulary's own
        # changes and offered as the default, instead of a full re-pick.
        default = []
        if existing_subjects:
            from crsw_deposit import authority
            mapped, applied = authority.map_subjects(existing_subjects, vocab_dict)
            default = [t for t in mapped
                       if t in vocab.all_terms(vocab_dict)]
            if default and applied:
                for a in applied:
                    say("The vocabulary changed since this dataset was "
                        "described: %s -> %s%s" % (
                            ", ".join(a["from"]),
                            ", ".join(a["to"]) or "(removed)",
                            " (a split - check which apply)"
                            if a["kind"] == "split_review" else ""))
            else:
                warn("the existing record's subjects are no longer all in the "
                     "vocabulary - please choose again.")
        entries = subject_entries(vocab_dict)
        width = shutil.get_terminal_size().columns
        for line in subject_listing_lines(entries, width,
                                          subject_depths(vocab_dict)):
            say(line)
        say("")
        while True:
            if default:
                raw = input(style("Subjects - select one or more "
                                  "(comma-separated numbers or terms) "
                                  "[Enter keeps %s]: " % ", ".join(default),
                                  "bold"))
                if not raw.strip():
                    raw = ", ".join(default)
            else:
                raw = input(style("Subjects - select one or more "
                                  "(comma-separated numbers or terms): ", "bold"))
            subjects, unknown = resolve_subjects(raw, entries)
            if unknown:
                say(MESSAGES["unknown_subject"].format(terms=", ".join(unknown)))
                continue
            if not subjects:
                say("At least one subject term is required.")
                continue
            say("Subjects: %s" % ", ".join(subjects))
            meta["subject"] = subjects
            break

    if existing and existing.get("abstract") and not ask_yes_no(
            "Update the abstract?", default_no=True):
        meta["abstract"] = existing["abstract"]
    else:
        while True:
            say("Abstract (at least 50 words; single line, or paste and "
                "press Enter):")
            abstract = input("> ").strip()
            w = record.abstract_warning(abstract)
            # Soft gate, deliberate for the testing phase (r6 §0) -
            # revisit before the pilot whether it becomes a block.
            if w:
                warn(w)
                if not ask_yes_no("Continue anyway?", default_no=True):
                    continue
            meta["abstract"] = abstract
            break

    if existing:
        # Recommended fields carry over untouched; a metadata edit is a
        # deliberate act, not a toll on every deposit.
        # (derived_from is not copied: deposit_logic.assemble_record keeps
        # the existing list unless this deposit supplies one, r8 §3.)
        for field in ("license", "source_type", "source_detail", "creator"):
            if existing.get(field):
                meta[field] = existing[field]
    else:
        default_license = ("internal-only" if meta["sensitivity"] == "amber"
                           else "CC-BY-4.0")
        meta["license"] = ask("Licence", default=default_license)

        meta["source_type"] = ask_select("Source type", SOURCE_TYPE_ENTRIES)
        if meta["source_type"] == "other":
            say("Please be specific - 'other' with a vague detail is "
                "unfindable later.")
        detail = ask("Source detail (free text, e.g. name, URL, or "
                     "collection reference)")
        if len(detail) < 10:
            warn("'%s' will not help anyone in five years - consider naming "
                 "the archive, URL, or reference." % detail)
        meta["source_detail"] = detail
        if getattr(args, "provenance_doc", None) is None:
            prompt_origin(meta, vocab_dict)

    if getattr(args, "provenance_doc", None) is not None:
        provenance, derived_from = args.provenance_doc
        if provenance:
            meta["provenance"] = provenance
        if derived_from:
            meta["derived_from"] = derived_from

    if "creator" not in meta:
        creator = input("Creator - person or team intellectually "
                        "responsible for the dataset (Enter to skip): ").strip()
        if creator:
            meta["creator"] = creator

    if "steward" not in meta and not existing:
        steward = input("Domain steward (Enter to skip): ").strip()
        if steward:
            meta["steward"] = steward

    meta["vocabulary_version"] = vocab_dict.get("vocabulary_version")
    return meta, existing


def prompt_per_file_overrides(sources: List[Source], meta: Dict) -> Dict:
    """Ask once whether the shared coverage dates apply to all; per-file
    prompts if not. Overrides land in the manifest entries, keyed by
    member path (r7 §1) rather than local filename, so two files with
    the same basename in different sub-folders don't collide here
    either. The dataset envelope is then computed, not asked for (r5
    Q5). The per-file abstract retired with the per-file sidecar - one
    dataset, one abstract."""
    if len(sources) <= 1:
        return {}
    if ask_yes_no("Apply the same coverage dates to all %d files?"
                  % len(sources), default_no=False):
        return {}
    overrides = {}
    for src in sources:
        say("\n%s:" % src.member)
        o = {}
        for label, field in (("  Coverage start", "coverage_start"),
                             ("  Coverage end", "coverage_end")):
            while True:
                value = ask(label, default=meta[field])
                err = record.coverage_error(value)
                if err:
                    say("  " + err)
                    continue
                o[field] = value
                break
        # Only a range that differs from the shared one becomes a
        # per-file temporal; both ends are kept so the nested shape is
        # complete (r6 §3.1).
        if (o.get("coverage_start") != meta["coverage_start"]
                or o.get("coverage_end") != meta["coverage_end"]):
            overrides[src.member] = o
    return overrides


def confirm_filenames(sources: List[Source]) -> Optional[Dict]:
    """Offer corrections for problem member paths (advisory tier, one
    problem set per segment - r7 §1). Returns {orig_member: new_member}
    or None if the user aborts. NEVER auto-applies (spec §4.3); the
    local file and its folder are never touched, only the object name."""
    renames = {}
    for src in sources:
        problems = keys.member_path_problems(src.member)
        if not problems:
            continue
        suggestion = keys.suggest_member_path(src.member)
        say("\n%r %s." % (src.member, "; ".join(problems)))
        say("Suggested object name: %r (your local file is not renamed)" % suggestion)
        choice = ask_choice("  [a]ccept suggestion / [e]dit / [k]eep as-is / [q]uit",
                            ("a", "e", "k", "q"))
        if choice == "q":
            return None
        if choice == "a":
            renames[src.member] = suggestion
        elif choice == "e":
            renames[src.member] = ask("  Object filename")
    return renames


# ------------------------------------------------------------------ main

def preflight(args) -> Tuple[Optional[str], List[str]]:
    """Run spec §8 checks in order. Returns (rclone_path, failures).
    Failures are already-translated message strings."""
    failures = []
    rclone = transfer.find_rclone()
    if not rclone:
        return None, [MESSAGES["no_rclone"]]
    try:
        names = transfer.remote_names(rclone)
    except transfer.TransferError as e:
        return rclone, [MESSAGES["unknown"] + _detail(args, e)]
    if args.remote not in names:
        return rclone, [MESSAGES["no_remote"].format(
            remote=args.remote, remotes=", ".join(names) or "none yet")]
    kind = transfer.check_access(rclone, args.remote, args.bucket)
    if kind:
        failures.append(MESSAGES[kind])
    return rclone, failures


def _detail(args, e: transfer.TransferError) -> str:
    return ("\n\n--- rclone output ---\n" + e.detail) if args.verbose and e.detail else ""


PROGRESS_THRESHOLD = 100 * 1024 * 1024  # show rclone progress at >=100MB


def append_log(line: str) -> None:
    """One line per deposit, appended to <cache_dir>/deposits.log.
    Best-effort: logging failure never fails a deposit."""
    try:
        d = vocab.cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(str(d / "deposits.log"), "a", encoding="utf-8",
                 newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _verify_stored_size(rclone, args, key: str, expected: int) -> None:
    """Size comparison, never checksum-vs-ETag (r2 §0)."""
    entry = transfer.stat_key(rclone, args.remote, args.bucket, key)
    if entry is None:
        raise transfer.TransferError("verify_failed", "no object at %s" % key)
    if entry.get("Size") != expected:
        raise transfer.TransferError(
            "verify_failed",
            "size mismatch at %s: expected %d, stored %s"
            % (key, expected, entry.get("Size")))


def perform_deposits(rclone, args, plans, meta, entries, existing,
                     depositor, vocab_dict) -> int:
    """Members first, the record last (r5 Q4): the record's presence marks
    a complete deposit, so an interrupted batch leaves the prefix visibly
    incomplete instead of describing files that never landed. Not atomic
    (spec §7): stop on first failure and report exactly what landed.
    `entries` are the precomputed manifest entries, aligned with `plans`;
    `existing` is the parsed current record or None (first deposit)."""
    dataset_uuid = deposit_logic.dataset_uuid_for(existing)
    existing_files = existing.get("files") if existing else None
    skip = record.unchanged_paths(existing_files, entries)

    def headers_for(checksum: str) -> Dict[str, str]:
        # The four labels, in rclone's --header-upload spelling.
        return labels_mod.as_s3_headers(labels_mod.object_labels(
            dataset_uuid, checksum, meta["sensitivity"], depositor))

    prefix = deposit_logic.dataset_prefix(meta)

    done = []
    skipped = []
    failed = None      # (stage name, message)
    interrupted = False
    record_written = False
    current = "planning"

    with tempfile.TemporaryDirectory(prefix="crsw-deposit-") as tmp:
        try:
            for i, (plan, entry) in enumerate(zip(plans, entries), 1):
                current = plan["member"]
                say("\n[%d/%d] %s" % (i, len(plans), plan["member"]))
                if entry["path"] in skip:
                    say("  skipped (unchanged): %s" % plan["key"])
                    skipped.append(plan)
                    continue
                size = plan["path"].stat().st_size
                show = size >= PROGRESS_THRESHOLD and sys.stdout.isatty()
                say("  uploading (%s)..." % human_size(size))
                transfer.copyto(rclone, plan["path"], args.remote, args.bucket,
                                plan["key"], show_progress=show,
                                headers=headers_for(entry["checksum_sha256"]))
                say("  verifying...")
                _verify_stored_size(rclone, args, plan["key"], size)
                append_log("%s\t%s\t%s\t%s" % (
                    record.utc_now_iso(), plan["key"],
                    entry["checksum_sha256"], depositor))
                done.append(plan)
                say(style("  done: %s" % plan["key"], "green"))

            # All members are in place - assemble and write the record.
            # Envelope widening, created/modified, depositor accumulation
            # all live in deposit_logic so the web route does the same.
            rec, union, added, updated = deposit_logic.assemble_record(
                meta, existing, entries, depositor,
                now=record.utc_now_iso(), dataset_uuid=dataset_uuid)
            errors, _ = record.validate_record(
                rec, vocab.all_terms(vocab_dict),
                vocab.domain_codes(vocab_dict),
                vocab.activity_codes(vocab_dict))
            if errors:
                raise transfer.TransferError("record_invalid",
                                             "; ".join(errors))

            record_filename = keys.record_filename(meta["dataset"])
            current = record_filename
            say("\nwriting dataset record...")
            record_path = Path(tmp) / record_filename
            # Written as bytes, never text: the record's own checksum
            # label is computed from these exact bytes, which used to
            # differ by platform newline translation (r7 §4).
            # deposit_logic.record_bytes is the one serialiser.
            record_path.write_bytes(deposit_logic.record_bytes(rec))
            record_key = prefix + "/" + record_filename
            record_checksum = record.sha256_file(record_path)
            transfer.copyto(rclone, record_path, args.remote, args.bucket,
                            record_key, headers=headers_for(record_checksum))
            text, err = transfer.read_key(rclone, args.remote, args.bucket,
                                          record_key)
            if text is None:
                raise transfer.TransferError(
                    "verify_failed", "record re-read failed: %s" % err)
            try:
                record.parse_record(text)
            except record.RecordParseError as e:
                raise transfer.TransferError(
                    "verify_failed", "record round-trip: %s" % e)
            append_log("%s\t%s\t%s\t%s" % (
                record.utc_now_iso(), record_key,
                record_checksum, depositor))
            record_written = True

            # Completion check (r5 Q4): every manifest entry - including
            # skipped and legacy members - exists at its key at size.
            current = "manifest verification"
            say("verifying the manifest against the store...")

            def stored_size(key):
                entry = transfer.stat_key(rclone, args.remote, args.bucket, key)
                return None if entry is None else entry.get("Size")

            problems = deposit_logic.completion_problems(prefix, union,
                                                         stored_size)
            if problems:
                raise transfer.TransferError("verify_failed", problems[0])
        except transfer.TransferError as e:
            failed = (current, MESSAGES.get(e.kind, MESSAGES["unknown"])
                      + _detail(args, e))
        except OSError as e:
            failed = (current, "Could not read %s: %s" % (current, e))
        except KeyboardInterrupt:
            interrupted = True

    # ------- report (spec §7 step 10): what landed, what didn't, what next.
    REPORT_LIMIT = 20
    say("\n" + "=" * 60)
    if done:
        say("Uploaded %d file(s):" % len(done))
        for plan in done[:REPORT_LIMIT]:
            say("  %s" % plan["key"])
        if len(done) > REPORT_LIMIT:
            say("  ... and %d more (see deposits.log)"
               % (len(done) - REPORT_LIMIT))
    if skipped:
        say("Skipped %d unchanged file(s)." % len(skipped))
    remaining = [p for p in plans
                 if p not in done and p not in skipped]
    if failed is not None:
        name, message = failed
        say("\nFAILED on %s:" % name)
        say(message)
    if interrupted:
        say("\nInterrupted.")
    if (failed is not None or interrupted) and not record_written:
        if remaining:
            say("\nNot deposited: %s"
                % ", ".join(p["member"] for p in remaining))
        say("No record was written - the dataset record still describes "
            "the last complete deposit. Re-running the same command is "
            "safe: finished files are skipped and the record is written "
            "once everything is in place.")
    if failed is None and not interrupted:
        say(style("\ndataset %s %s: %d file(s), %d added, %d updated, "
                  "%d unchanged."
                  % (meta["project"], meta["version"], len(union),
                     len(added), len(updated), len(skipped)), "green"))
        return 0
    return 130 if interrupted else 1


def main(argv=None) -> int:
    enable_vt()
    args = build_parser().parse_args(argv)

    if args.sensitivity == "red":
        return fail(MESSAGES["red_refused"], 2)
    if args.sensitivity and args.sensitivity not in keys.SENSITIVITIES:
        return fail("sensitivity must be green or amber, got %r" % args.sensitivity)

    # Remote/bucket: flag -> config file -> built-in default (r2 §6).
    cfg = load_config()
    run_setup = args.reconfigure or (
        not cfg and (args.remote is None or args.bucket is None))
    if run_setup:
        rclone_for_setup = transfer.find_rclone()
        if rclone_for_setup:
            cfg = first_run_setup(rclone_for_setup)
        # no rclone -> preflight will fail with the no_rclone message anyway
    # Captured BEFORE resolve_settings overwrites args.remote/args.bucket -
    # the same ordering hazard setting_sources already documents - so
    # offer_to_save_settings below can tell "flag given" from "resolved
    # from config/default".
    flagged_remote, flagged_bucket = args.remote, args.bucket
    remote_src, bucket_src = setting_sources(args, cfg)
    args.remote, args.bucket = resolve_settings(args, cfg)

    # Say where every deposit is headed and why - so "which remote did it
    # use?" is answered by the output, not by reading the source.
    detail = "remote: %s; bucket: %s" % (remote_src, bucket_src)
    if "saved config" in (remote_src, bucket_src):
        detail += "; config: %s" % config_path()
    say("Target: %s  (%s)"
        % (style("%s:%s" % (args.remote, args.bucket), "bold"), detail))

    # Local checks first: files readable? No reserved names?
    sources, problems, notes = resolve_files(args.files,
                                             include_noise=args.include_noise)
    for p in problems:
        warn(p)
    for n in notes:
        say("note: " + n)
    if not sources:
        return fail("No files to deposit.")
    reserved = next((s.member for s in sources
                     if keys.is_reserved_member(s.member)), None)
    if reserved:
        return fail(MESSAGES["reserved_name"].format(name=reserved))
    total = sum(s.path.stat().st_size for s in sources)
    say("%d file(s), %s total." % (len(sources), human_size(total)))

    # Remote preflight (§8). Dry-run downgrades failures to warnings so the
    # preview still works offline - it's the support reproduction path.
    rclone, failures = preflight(args)
    if failures:
        if args.dry_run:
            for f in failures:
                warn("preflight: " + f.splitlines()[0] + " (continuing: --dry-run)")
        else:
            return fail("\n\n".join(failures))
    remote_ok = not failures

    # Offer to persist --remote/--bucket only once they've been proven
    # reachable and writable - otherwise the tool would happily save a
    # typo. Skipped entirely under --dry-run, on a non-tty, or when
    # first-run/--reconfigure setup already saved this run's values.
    if remote_ok:
        offer_to_save_settings(flagged_remote, flagged_bucket, cfg,
                               args.remote, args.bucket, run_setup,
                               args.dry_run)

    # Vocabulary (§6): warn once, non-fatally, naming the source used.
    vocab_dict, source = vocab.load_vocabulary()
    if source != "remote":
        warn("could not fetch the live vocabulary; using the %s copy "
             "(version %s). Deposits still work - very new terms may be missing."
             % (source, vocab_dict.get("vocabulary_version")))

    lister = ((lambda prefix: transfer.list_dirs(
                   rclone, args.remote, args.bucket, prefix))
              if (rclone and remote_ok) else None)
    prober = ((lambda prefix: transfer.check_write(
                   rclone, args.remote, args.bucket, prefix))
              if (rclone and remote_ok and not args.dry_run) else None)
    fetcher = ((lambda key: transfer.read_key(
                    rclone, args.remote, args.bucket, key))
               if (rclone and remote_ok) else None)

    args.provenance_doc = None
    if args.provenance:
        try:
            args.provenance_doc = deposit_logic.parse_provenance_file(
                Path(args.provenance).read_text(encoding="utf-8"))
        except OSError as e:
            return fail("Could not read %s: %s" % (args.provenance, e), 2)
        except ValueError as e:
            return fail("--provenance: %s" % e, 2)

    try:
        meta, existing = prompt_metadata(args, vocab_dict, lister, prober,
                                         fetcher)
    except keys.RedDataError:
        return fail(MESSAGES["red_refused"], 2)
    except record.RecordParseError as e:
        return fail(MESSAGES["bad_record"].format(
            key="%s:%s/%s" % (args.remote, args.bucket,
                              getattr(e, "key", "the dataset record")),
            reason=e))
    except ValueError as e:
        return fail(str(e))
    except transfer.TransferError as e:
        if e.kind == "permission":
            return fail(MESSAGES["write_denied"].format(
                target="%s:%s/%s/" % (args.remote, args.bucket, e.detail)))
        return fail(MESSAGES.get(e.kind, MESSAGES["unknown"]))

    per_file = prompt_per_file_overrides(sources, meta)

    renames = confirm_filenames(sources)
    if renames is None:
        say("Nothing deposited.")
        return 0

    # Apply accepted object-name corrections to planning only (local files
    # are never touched). A rename into the reserved record name, or into
    # a key another batch file already claims, is refused, not crashed.
    try:
        plans = plan_deposits(sources, meta, per_file)
        seen = {plan["key"]: plan["path"] for plan in plans}
        for plan in plans:
            if plan["member"] not in renames:
                continue
            new_member = renames[plan["member"]]
            del seen[plan["key"]]
            new_key = keys.build_key(
                meta["strand"], meta["project"], meta["sensitivity"],
                meta["state"], meta["dataset"], new_member)
            if new_key in seen:
                raise ValueError(
                    "%s and %s would land at the same key (%s) - rename "
                    "one and re-run" % (seen[new_key], plan["path"], new_key))
            seen[new_key] = plan["path"]
            # key and member must be rewritten together (set_member) -
            # a plan whose two disagree fails the completion check only
            # after every file has uploaded (r7 §1).
            set_member(plan, meta, new_member)
    except ValueError as e:
        return fail(str(e))

    # (Write permission was already probed on the full deposit prefix
    # during prompt_metadata - spec §8: fail early. Overwrites need no
    # separate prompt: re-runs are idempotent by design (r5 Q4) and the
    # preview labels updated members before the final confirmation.)

    say("\nComputing checksums for the manifest...")
    try:
        entries = prepare_entries(plans)
    except OSError as e:
        return fail("Could not read a file while checksumming: %s" % e)
    classification = classify_members(entries, existing)
    for w in duplicate_content_warnings(entries, existing):
        warn(w)

    say("\nPlanned deposit:")
    for line in preview_lines(plans, meta, existing, classification):
        say(line)

    if args.dry_run:
        say("\n--dry-run: nothing was uploaded, no temp files were written.")
        return 0

    if not ask_yes_no("\nDeposit %d file(s) to %s:%s? This is the point of "
                      "no return." % (len(plans), args.remote, args.bucket)):
        say("Nothing deposited.")
        return 0

    return perform_deposits(rclone, args, plans, meta, entries, existing,
                            record.default_depositor(), vocab_dict)


if __name__ == "__main__":
    sys.exit(main())
