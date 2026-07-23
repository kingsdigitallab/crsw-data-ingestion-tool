#!/usr/bin/env python3
"""CRSW deposit tool - CLI front end.

ALL user interaction lives in this file: argparse, prompting, preview,
confirmation, progress, and translation of structured errors into
human-readable messages. The logic modules (keys, sidecar, vocab,
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
import sidecar
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
        "Could not find rclone. Looked on your PATH, then for ./rclone and\n"
        "./rclone.exe next to this script.\n"
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
        "Cannot reach the storage endpoint. The usual cause is the KCL VPN -\n"
        "check you are connected to it and try again. If the VPN is up and\n"
        "this persists, contact eResearch."),
    "credentials": (
        "The storage service rejected your credentials. If your access keys\n"
        "are new or were recently rotated, the config may be out of date -\n"
        "contact eResearch to confirm your keys."),
    "permission": (
        "You don't have write access to this location. Access is scoped by\n"
        "strand, so an RS2 credential cannot write to rs3/ - this is a\n"
        "permissions question, not a bug. If you believe you should have\n"
        "access to this strand, contact eResearch."),
    "not_found": (
        "The bucket or path was not found on the storage service. Check the\n"
        "--bucket value (default: crsw); if it looks right, contact eResearch."),
    "unknown": (
        "The transfer failed for an unrecognised reason. Re-run with\n"
        "--verbose and send the output to eResearch support."),
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
    "collision": (
        "An object already exists at {key}.\n"
        "Depositing will create a NEW VERSION of that object (the old version\n"
        "stays recoverable via bucket versioning). It will not create a\n"
        "second file."),
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
    """Pure planning: one dict per file with its key, sidecar key and fields.
    per_file maps filename -> {field: value} overrides (coverage/abstract)."""
    plans = []
    for path in files:
        fields = dict(
            strand=meta["strand"], domain=meta["domain"],
            project=meta["project"], state=meta["state"],
            sensitivity=meta["sensitivity"],
            coverage_start=meta["coverage_start"],
            coverage_end=meta["coverage_end"],
            version=meta["version"], abstract=meta["abstract"],
            subjects=list(meta["subjects"]),
            vocabulary_version=meta.get("vocabulary_version"),
            source_type=meta.get("source_type"),
            source_detail=meta.get("source_detail"),
            derived_from=meta.get("derived_from"),
            licence=meta.get("licence"), steward=meta.get("steward"),
        )
        fields.update(per_file.get(path.name, {}))
        key = keys.build_key(fields["strand"], fields["project"],
                             fields["state"], fields["sensitivity"], path.name)
        fields["object_key"] = key
        plans.append({"path": path, "key": key,
                      "sidecar_key": keys.sidecar_key(key), "fields": fields})
    return plans


def preview_lines(plans: List[Dict], limit: int = 3) -> List[str]:
    lines = []
    for plan in plans[:limit]:
        f = plan["fields"]
        lines.append("  %s" % style(plan["key"], "cyan"))
        lines.append("    sidecar: %s" % plan["sidecar_key"])
        if f:
            lines.append("    coverage %s to %s | %s | subjects: %s" % (
                f.get("coverage_start"), f.get("coverage_end"),
                f.get("version"), ", ".join(f.get("subjects", []))))
    if len(plans) > limit:
        lines.append("  ... and %d more" % (len(plans) - limit))
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deposit.py",
        description="Deposit research files into CRSW shared storage with "
                    "sidecar metadata.")
    p.add_argument("files", nargs="+", metavar="FILE_OR_GLOB")
    p.add_argument("--strand", choices=keys.STRANDS)
    p.add_argument("--project")
    p.add_argument("--domain", help="data domain code (see vocabulary)")
    p.add_argument("--state", choices=keys.STATES)
    p.add_argument("--sensitivity")  # validated by hand so 'red' gets OUR message
    p.add_argument("--dry-run", action="store_true",
                   help="preview keys and sidecars, upload nothing")
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

def prompt_metadata(args, existing_projects: List[str], vocab_dict: Dict) -> Dict:
    """Collect batch metadata, honouring flags. Returns the meta dict."""
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

    if args.project:
        project = args.project
        if not keys.validate_project(project):
            raise ValueError(
                "project %r: use lowercase letters/digits and hyphens "
                "(e.g. csac, treaty-texts)" % project)
    else:
        if existing_projects:
            say("Existing projects in %s: %s" % (
                meta["strand"], ", ".join(existing_projects)))
        while True:
            project = ask("Project").lower()
            if not keys.validate_project(project):
                say("Project names are lowercase letters/digits with hyphens "
                    "(e.g. csac, treaty-texts). Try again.")
                continue
            if existing_projects and project not in existing_projects:
                if not ask_yes_no(
                        "'%s' is a NEW project prefix (existing: %s). Create it?"
                        % (project, ", ".join(existing_projects))):
                    continue
            break
    meta["project"] = project

    meta["state"] = args.state or ask_select("State", STATE_ENTRIES)

    domain_entries = vocab.domains(vocab_dict)
    codes = [d["code"] for d in domain_entries]
    if args.domain:
        if args.domain not in codes:
            raise ValueError("domain %r is not one of: %s"
                             % (args.domain, ", ".join(codes)))
        meta["domain"] = args.domain
    else:
        meta["domain"] = ask_select(
            "Domain", [(str(i), d["code"], d["label"])
                       for i, d in enumerate(domain_entries, 1)])
    chosen = next(d for d in domain_entries if d["code"] == meta["domain"])
    if chosen["steward"] and chosen["steward"] != "TBC":
        meta["steward"] = chosen["steward"]
        say("Steward: %s (from domain %s)" % (chosen["steward"], meta["domain"]))

    while True:
        version = sidecar.normalise_version(ask("Version", default="1-0"))
        if version is not None:
            meta["version"] = version
            break
        say("Version must be two integers like 3-0 (or 3.0 / v3-0, "
            "which I will normalise).")

    for label, field in (("Coverage start (year or YYYY-MM-DD)", "coverage_start"),
                         ("Coverage end (year or YYYY-MM-DD)", "coverage_end")):
        while True:
            value = ask(label)
            err = sidecar.coverage_error(value)
            if err:
                say(err)
                continue
            meta[field] = value
            break

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
        meta["subjects"] = subjects
        break

    say("Abstract (100-300 words; single line, or paste and press Enter):")
    abstract = input("> ").strip()
    w = sidecar.abstract_warning(abstract)
    if w:
        warn(w + " - recorded anyway; you can revise the sidecar later.")
    meta["abstract"] = abstract

    default_licence = "internal-only" if meta["sensitivity"] == "amber" else "CC-BY-4.0"
    meta["licence"] = ask("Licence", default=default_licence)

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

    if "steward" not in meta:
        steward = input("Domain steward (Enter to skip): ").strip()
        if steward:
            meta["steward"] = steward

    meta["vocabulary_version"] = vocab_dict.get("vocabulary_version")
    return meta


def prompt_per_file_overrides(files: List[Path], meta: Dict) -> Dict:
    """Ask once whether shared abstract/dates apply to all; per-file prompts
    if not. Coverage dates are the most likely to differ (spec §7)."""
    if len(files) <= 1:
        return {}
    if ask_yes_no("Apply the same abstract and coverage dates to all %d files?"
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
                err = sidecar.coverage_error(value)
                if err:
                    say("  " + err)
                    continue
                if value != meta[field]:
                    o[field] = value
                break
        new_abstract = input(
            "  Abstract (Enter to keep the shared one): ").strip()
        if new_abstract:
            o["abstract"] = new_abstract
        if o:
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


def perform_deposits(rclone, args, plans, depositor) -> int:
    """Upload each plan: checksum -> sidecar to temp -> data -> sidecar ->
    verify. Not atomic (spec §7): stop on first failure and report exactly
    what landed. Temp files always cleaned up (TemporaryDirectory)."""
    done = []
    failed = None      # (plan, message)
    interrupted = False

    with tempfile.TemporaryDirectory(prefix="crsw-deposit-") as tmp:
        try:
            for i, plan in enumerate(plans, 1):
                say("\n[%d/%d] %s" % (i, len(plans), plan["path"].name))

                say("  computing checksum...")
                checksum = sidecar.sha256_file(plan["path"])

                fields = dict(plan["fields"])
                fields["checksum_sha256"] = checksum
                fields["depositor"] = depositor
                fields["deposited"] = sidecar.utc_now_iso()
                sc = sidecar.build_sidecar(**fields)

                sidecar_path = Path(tmp) / (plan["path"].name + ".meta.json")
                sidecar_path.write_text(sidecar.sidecar_json(sc), encoding="utf-8")

                size = plan["path"].stat().st_size
                show = size >= PROGRESS_THRESHOLD and sys.stdout.isatty()
                say("  uploading data (%s)..." % human_size(size))
                transfer.copyto(rclone, plan["path"], args.remote, args.bucket,
                                plan["key"], show_progress=show)
                say("  uploading sidecar...")
                transfer.copyto(rclone, sidecar_path, args.remote, args.bucket,
                                plan["sidecar_key"])

                say("  verifying...")
                for key, local in ((plan["key"], plan["path"]),
                                   (plan["sidecar_key"], sidecar_path)):
                    entry = transfer.stat_key(rclone, args.remote,
                                              args.bucket, key)
                    if entry is None:
                        raise transfer.TransferError(
                            "verify_failed", "no object at %s" % key)
                    expected = local.stat().st_size
                    if entry.get("Size") != expected:
                        raise transfer.TransferError(
                            "verify_failed",
                            "size mismatch at %s: local %d, stored %s"
                            % (key, expected, entry.get("Size")))

                append_log("%s\t%s\t%s\t%s" % (
                    sidecar.utc_now_iso(), plan["key"], checksum, depositor))
                done.append(plan)
                say(style("  done: %s" % plan["key"], "green"))
        except transfer.TransferError as e:
            failed = (plan, MESSAGES.get(e.kind, MESSAGES["unknown"])
                      + _detail(args, e))
        except OSError as e:
            failed = (plan, "Could not read %s: %s" % (plan["path"], e))
        except KeyboardInterrupt:
            interrupted = True

    # ------- report (spec §7 step 10): what landed, what didn't, what next.
    say("\n" + "=" * 60)
    if done:
        say("Deposited %d of %d file(s):" % (len(done), len(plans)))
        for plan in done:
            say("  %s" % plan["key"])
    remaining = [p for p in plans if p not in done]
    if failed is not None:
        bad_plan, message = failed
        say("\nFAILED on %s:" % bad_plan["path"].name)
        say(message)
    if interrupted:
        say("\nInterrupted.")
    if remaining and (failed is not None or interrupted):
        say("\nNot deposited: %s" % ", ".join(p["path"].name for p in remaining))
        say("Re-running the same command is safe: object keys are "
            "deterministic, so completed files are simply overwritten as a "
            "new version - nothing is duplicated.")
    if failed is None and not interrupted:
        say(style("\nAll %d file(s) deposited successfully." % len(done), "green"))
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
    args.remote, args.bucket = resolve_settings(args, cfg)

    # Local checks first: files readable?
    files, problems = resolve_files(args.files)
    for p in problems:
        warn(p)
    if not files:
        return fail("No files to deposit.")
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

    existing = (transfer.list_projects(rclone, args.remote, args.bucket, args.strand)
                if (rclone and remote_ok and args.strand) else [])

    try:
        meta = prompt_metadata(args, existing, vocab_dict)
    except keys.RedDataError:
        return fail(MESSAGES["red_refused"], 2)
    except ValueError as e:
        return fail(str(e))

    per_file = prompt_per_file_overrides(files, meta)

    renames = confirm_filenames(files)
    if renames is None:
        say("Nothing deposited.")
        return 0

    # Apply accepted object-name corrections to planning only (local files
    # are never touched).
    plans = plan_deposits(files, meta, per_file)
    for plan in plans:
        if plan["path"].name in renames:
            new_name = renames[plan["path"].name]
            plan["key"] = keys.build_key(
                meta["strand"], meta["project"], meta["state"],
                meta["sensitivity"], new_name)
            plan["sidecar_key"] = keys.sidecar_key(plan["key"])
            plan["fields"]["object_key"] = plan["key"]

    # Write-permission probe on the real prefix (needs strand+project).
    if remote_ok and not args.dry_run:
        prefix = "%s/%s" % (meta["strand"], meta["project"])
        kind = transfer.check_write(rclone, args.remote, args.bucket, prefix)
        if kind:
            return fail(MESSAGES[kind])

    # Collision check (§4): overwriting creates a new version, say so.
    if remote_ok:
        for plan in plans:
            if transfer.key_exists(rclone, args.remote, args.bucket, plan["key"]):
                say("\n" + MESSAGES["collision"].format(key=plan["key"]))
                if not args.dry_run and not ask_yes_no(
                        "Deposit a new version of %s?" % plan["path"].name):
                    say("Nothing deposited.")
                    return 0

    say("\nPlanned deposits:")
    for line in preview_lines(plans):
        say(line)

    if args.dry_run:
        say("\n--dry-run: nothing was uploaded, no temp files were written.")
        return 0

    if not ask_yes_no("\nDeposit %d file(s) to %s:%s? This is the point of "
                      "no return." % (len(plans), args.remote, args.bucket)):
        say("Nothing deposited.")
        return 0

    return perform_deposits(rclone, args, plans, sidecar.default_depositor())


if __name__ == "__main__":
    sys.exit(main())
