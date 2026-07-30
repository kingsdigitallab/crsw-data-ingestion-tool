#!/usr/bin/env python3
"""CRSW deposit tool - CLI front end.

ALL user interaction lives in this file: argparse, prompting, preview,
confirmation, progress, and translation of structured errors into
human-readable messages. The logic modules (keys, record, vocab,
transfer) have no interactive I/O so the future web gateway can import
them unchanged."""
import argparse
import glob as globlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import keys
import record
import transfer
import vocab

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
        "this script.\n"
        "Download it from %s and either install it or just drop the rclone\n"
        "binary in this folder - both work." % RCLONE_DOWNLOAD_URL),
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


def subject_listing_lines(entries, width):
    """Grouped-by-facet listing. Two columns at >=100 chars (r2 §4)."""
    columns = 2 if width >= 100 else 1
    num_w = max(len(n) for n, _t, _f in entries)
    term_w = max(len(t) for _n, t, _f in entries)
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
        cell = "%s %-*s  " % (style("[%*s]" % (num_w, n), "dim"), term_w, term)
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


def resolve_files(patterns: List[str]) -> Tuple[List[Path], List[str]]:
    """Expand globs; reject directories and misses. Returns (files, problems)."""
    files = []
    problems = []
    for pattern in patterns:
        matches = globlib.glob(pattern)
        if not matches:
            problems.append("no file matches %r" % pattern)
            continue
        for m in matches:
            p = Path(m)
            if p.is_dir():
                problems.append(
                    "%s is a directory - deposit files individually or with a "
                    "glob (there is no --recursive yet)" % p)
            elif p.is_file():
                files.append(p)
            else:
                problems.append("cannot read %s" % p)
    return files, problems


def plan_deposits(files: List[Path], meta: Dict, per_file: Dict) -> List[Dict]:
    """Pure planning: one dict per file with its object key and manifest
    overrides. per_file maps filename -> coverage overrides (r5 Q5).
    Raises ValueError when two files would land at the same key - that
    would be a silent overwrite inside one batch."""
    plans = []
    seen = {}
    for path in files:
        key = keys.build_key(meta["strand"], meta["project"],
                             meta["sensitivity"], meta["state"],
                             meta["dataset"], path.name)
        if key in seen:
            raise ValueError(
                "%s and %s would land at the same key (%s) - rename one "
                "and re-run" % (seen[key], path, key))
        seen[key] = path
        plans.append({"path": path, "key": key,
                      "entry_overrides": dict(per_file.get(path.name, {}))})
    return plans


def object_name(plan: Dict) -> str:
    """The deposited filename: the key's last segment (renames included)."""
    return plan["key"].rsplit("/", 1)[-1]


def prepare_entries(plans: List[Dict]) -> List[Dict]:
    """Checksum and stat every file into its manifest entry (r5 §2),
    aligned with `plans` by index. Reads local files; never uploads."""
    entries = []
    for plan in plans:
        name = object_name(plan)
        overrides = plan.get("entry_overrides", {})
        entries.append(record.manifest_entry(
            name,
            record.sha256_file(plan["path"]),
            plan["path"].stat().st_size,
            temporal=(record.temporal_object(overrides.get("coverage_start"),
                                             overrides.get("coverage_end"))
                      if overrides.get("coverage_start")
                      or overrides.get("coverage_end") else None),
            fmt=record.guess_format(name)))
    return entries


def classify_members(entries: List[Dict], existing: Optional[Dict]):
    """(added, updated, unchanged) paths for the batch against the
    existing record, if any (r5 Q4). Drives the preview and the report."""
    existing_files = existing.get("files") if existing else None
    _, added, updated = record.merge_manifest(existing_files, entries)
    unchanged = sorted(record.unchanged_paths(existing_files, entries))
    return added, updated, unchanged


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
        name = object_name(plan)
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
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deposit.py",
        description="Deposit research files into CRSW shared storage, "
                    "described by one dataset record per prefix.")
    p.add_argument("files", nargs="+", metavar="FILE_OR_GLOB")
    p.add_argument("--strand", choices=keys.STRANDS)
    p.add_argument("--project")
    p.add_argument("--dataset", help="dataset name within the project "
                   "(r6: a project may hold several datasets)")
    p.add_argument("--domain", help="data domain code (see vocabulary)")
    p.add_argument("--state", choices=keys.STATES)
    p.add_argument("--sensitivity")  # validated by hand so 'red' gets OUR message
    p.add_argument("--dry-run", action="store_true",
                   help="preview keys and the dataset record, upload nothing")
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
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
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
        if existing_subjects:
            warn("the existing record's subjects are no longer all in the "
                 "vocabulary - please choose again.")
        entries = subject_entries(vocab_dict)
        width = shutil.get_terminal_size().columns
        for line in subject_listing_lines(entries, width):
            say(line)
        say("")
        while True:
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
        for field in ("license", "source_type", "source_detail",
                      "derived_from", "creator"):
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
        if meta["source_type"] == "derived":
            parent = input("Parent object key, if known (Enter to skip): ").strip()
            if parent:
                meta["derived_from"] = parent

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


def prompt_per_file_overrides(files: List[Path], meta: Dict) -> Dict:
    """Ask once whether the shared coverage dates apply to all; per-file
    prompts if not. Overrides land in the manifest entries; the dataset
    envelope is then computed, not asked for (r5 Q5). The per-file
    abstract retired with the per-file sidecar - one dataset, one
    abstract."""
    if len(files) <= 1:
        return {}
    if ask_yes_no("Apply the same coverage dates to all %d files?"
                  % len(files), default_no=False):
        return {}
    overrides = {}
    for path in files:
        say("\n%s:" % path.name)
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
            overrides[path.name] = o
    return overrides


def confirm_filenames(files: List[Path]) -> Optional[Dict]:
    """Offer corrections for problem filenames. Returns {orig_name: deposit_name}
    or None if the user aborts. NEVER auto-applies (spec §4.3)."""
    renames = {}
    for path in files:
        problems = keys.filename_problems(path.name)
        if not problems:
            continue
        suggestion = keys.suggest_filename(path.name)
        say("\n%r %s." % (path.name, "; ".join(problems)))
        say("Suggested object name: %r (your local file is not renamed)" % suggestion)
        choice = ask_choice("  [a]ccept suggestion / [e]dit / [k]eep as-is / [q]uit",
                            ("a", "e", "k", "q"))
        if choice == "q":
            return None
        if choice == "a":
            renames[path.name] = suggestion
        elif choice == "e":
            renames[path.name] = ask("  Object filename")
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
        with open(str(d / "deposits.log"), "a", encoding="utf-8") as f:
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
    dataset_uuid = (existing["dataset_uuid"] if existing
                    else record.mint_uuid())
    existing_files = existing.get("files") if existing else None
    skip = record.unchanged_paths(existing_files, entries)
    base_labels = {
        "x-amz-meta-dataset-uuid": dataset_uuid,
        "x-amz-meta-sensitivity": meta["sensitivity"],
        "x-amz-meta-depositor": depositor,
    }
    prefix = keys.dataset_prefix(meta["strand"], meta["project"],
                                 meta["sensitivity"], meta["state"],
                                 meta["dataset"])

    done = []
    skipped = []
    failed = None      # (stage name, message)
    interrupted = False
    record_written = False
    current = "planning"

    with tempfile.TemporaryDirectory(prefix="crsw-deposit-") as tmp:
        try:
            for i, (plan, entry) in enumerate(zip(plans, entries), 1):
                current = plan["path"].name
                say("\n[%d/%d] %s" % (i, len(plans), plan["path"].name))
                if entry["path"] in skip:
                    say("  skipped (unchanged): %s" % plan["key"])
                    skipped.append(plan)
                    continue
                size = plan["path"].stat().st_size
                show = size >= PROGRESS_THRESHOLD and sys.stdout.isatty()
                labels = dict(base_labels)
                labels["x-amz-meta-checksum-sha256"] = entry["checksum_sha256"]
                say("  uploading (%s)..." % human_size(size))
                transfer.copyto(rclone, plan["path"], args.remote, args.bucket,
                                plan["key"], show_progress=show,
                                headers=labels)
                say("  verifying...")
                _verify_stored_size(rclone, args, plan["key"], size)
                append_log("%s\t%s\t%s\t%s" % (
                    record.utc_now_iso(), plan["key"],
                    entry["checksum_sha256"], depositor))
                done.append(plan)
                say(style("  done: %s" % plan["key"], "green"))

            # All members are in place - assemble and write the record.
            union, added, updated = record.merge_manifest(existing_files,
                                                          entries)
            now = record.utc_now_iso()
            pairs = [record.temporal_pair(e.get("temporal")) for e in union]
            if existing:
                # The old envelope keeps containment for legacy members
                # whose per-file coverage was never recorded.
                pairs.append(record.temporal_pair(existing.get("temporal")))
            cov_start, cov_end = record.widen(
                (meta["coverage_start"], meta["coverage_end"]), pairs)
            rec = record.build_record(
                dataset_uuid=dataset_uuid,
                identifier=prefix,
                strand=meta["strand"], domain=meta["domain"],
                project=meta["project"], dataset=meta["dataset"],
                state=meta["state"], sensitivity=meta["sensitivity"],
                temporal=record.temporal_object(cov_start, cov_end),
                version=meta["version"], abstract=meta["abstract"],
                subject=list(meta["subject"]), files=union,
                created=(existing or {}).get("created") or now,
                modified=now,
                vocabulary_version=meta.get("vocabulary_version"),
                creator=meta.get("creator"),
                source_type=meta.get("source_type"),
                source_detail=meta.get("source_detail"),
                license=meta.get("license"), steward=meta.get("steward"),
                depositors=record.append_depositor(
                    (existing or {}).get("depositors"), depositor),
                derived_from=meta.get("derived_from"))
            errors, _ = record.validate_record(
                rec, vocab.all_terms(vocab_dict),
                vocab.domain_codes(vocab_dict))
            if errors:
                raise transfer.TransferError("record_invalid",
                                             "; ".join(errors))

            record_filename = keys.record_filename(meta["dataset"])
            current = record_filename
            say("\nwriting dataset record...")
            record_path = Path(tmp) / record_filename
            record_path.write_text(record.record_json(rec), encoding="utf-8")
            record_key = prefix + "/" + record_filename
            labels = dict(base_labels)
            labels["x-amz-meta-checksum-sha256"] = record.sha256_file(record_path)
            transfer.copyto(rclone, record_path, args.remote, args.bucket,
                            record_key, headers=labels)
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
                labels["x-amz-meta-checksum-sha256"], depositor))
            record_written = True

            # Completion check (r5 Q4): every manifest entry - including
            # skipped and legacy members - exists at its key at size.
            current = "manifest verification"
            say("verifying the manifest against the store...")
            for entry in union:
                _verify_stored_size(rclone, args,
                                    prefix + "/" + entry["path"],
                                    entry["bytes"])
        except transfer.TransferError as e:
            failed = (current, MESSAGES.get(e.kind, MESSAGES["unknown"])
                      + _detail(args, e))
        except OSError as e:
            failed = (current, "Could not read %s: %s" % (current, e))
        except KeyboardInterrupt:
            interrupted = True

    # ------- report (spec §7 step 10): what landed, what didn't, what next.
    say("\n" + "=" * 60)
    if done:
        say("Uploaded %d file(s):" % len(done))
        for plan in done:
            say("  %s" % plan["key"])
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
                % ", ".join(p["path"].name for p in remaining))
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
    files, problems = resolve_files(args.files)
    for p in problems:
        warn(p)
    if not files:
        return fail("No files to deposit.")
    reserved = next((f.name for f in files if keys.is_reserved_name(f.name)),
                    None)
    if reserved:
        return fail(MESSAGES["reserved_name"].format(name=reserved))
    total = sum(f.stat().st_size for f in files)
    say("%d file(s), %s total." % (len(files), human_size(total)))

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

    per_file = prompt_per_file_overrides(files, meta)

    renames = confirm_filenames(files)
    if renames is None:
        say("Nothing deposited.")
        return 0

    # Apply accepted object-name corrections to planning only (local files
    # are never touched). A rename into the reserved record name, or into
    # a key another batch file already claims, is refused, not crashed.
    try:
        plans = plan_deposits(files, meta, per_file)
        seen = {plan["key"]: plan["path"] for plan in plans}
        for plan in plans:
            if plan["path"].name not in renames:
                continue
            new_name = renames[plan["path"].name]
            del seen[plan["key"]]
            new_key = keys.build_key(
                meta["strand"], meta["project"], meta["sensitivity"],
                meta["state"], meta["dataset"], new_name)
            if new_key in seen:
                raise ValueError(
                    "%s and %s would land at the same key (%s) - rename "
                    "one and re-run" % (seen[new_key], plan["path"], new_key))
            seen[new_key] = plan["path"]
            plan["key"] = new_key
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
