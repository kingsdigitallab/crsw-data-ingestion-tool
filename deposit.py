#!/usr/bin/env python3
"""CRSW deposit tool - CLI front end.

ALL user interaction lives in this file: argparse, prompting, preview,
confirmation, progress, and translation of structured errors into
human-readable messages. The logic modules (keys, sidecar, vocab,
transfer) have no interactive I/O so the future web gateway can import
them unchanged."""
import argparse
import glob as globlib
import os
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
        "section (keys come from eResearch):\n\n" + RCLONE_STANZA),
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
    "unknown_subject": (
        "These terms are not in the subjects vocabulary: {terms}\n"
        "Unknown terms can't be accepted (they would silently fragment the\n"
        "catalogue). To propose an addition, open an issue on the vocabulary\n"
        "repository or ask your domain steward. Valid terms are listed above."),
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
            source=meta.get("source"), licence=meta.get("licence"),
            steward=meta.get("steward"),
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
    p.add_argument("--state", choices=keys.STATES)
    p.add_argument("--sensitivity")  # validated by hand so 'red' gets OUR message
    p.add_argument("--dry-run", action="store_true",
                   help="preview keys and sidecars, upload nothing")
    p.add_argument("--remote", default="ceph", help="rclone remote name")
    p.add_argument("--bucket", default="crsw", help="target bucket")
    p.add_argument("--verbose", action="store_true",
                   help="show raw rclone output on errors")
    return p


# ---------------------------------------------------------------- prompts

def prompt_metadata(args, existing_projects: List[str], vocab_dict: Dict) -> Dict:
    """Collect batch metadata, honouring flags. Returns the meta dict."""
    meta = {}

    meta["strand"] = args.strand or ask_choice("Strand", keys.STRANDS)

    sensitivity = args.sensitivity or ask_choice(
        "Sensitivity", ("green", "amber", "red"))
    if sensitivity == "red":
        raise keys.RedDataError()
    if sensitivity not in keys.SENSITIVITIES:
        raise ValueError("sensitivity must be green or amber, got %r" % sensitivity)
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

    meta["state"] = args.state or ask_choice("State", keys.STATES)
    meta["domain"] = ask_choice("Domain", keys.DOMAINS)

    while True:
        version = ask("Version", default="v1-0")
        w = sidecar.version_warning(version)
        if w:
            say(w)
            if not ask_yes_no("Use %r anyway?" % version):
                continue
        meta["version"] = version
        break

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

    terms = vocab.all_terms(vocab_dict)
    while True:
        raw = ask("Subjects (comma-separated)")
        subjects = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = sidecar.unknown_subjects(subjects, terms)
        if not subjects:
            say("At least one subject term is required.")
            continue
        if unknown:
            say(MESSAGES["unknown_subject"].format(terms=", ".join(unknown)))
            for facet, facet_terms in sorted(vocab_dict["facets"].items()):
                say("  %s: %s" % (facet, ", ".join(sorted(facet_terms))))
            continue
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
    source = input("Source (archive/survey/scrape/partner org; Enter to skip): ").strip()
    if source:
        meta["source"] = source
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
        return rclone, [MESSAGES["no_remote"].format(remote=args.remote)]
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
                for key in (plan["key"], plan["sidecar_key"]):
                    if not transfer.key_exists(rclone, args.remote,
                                               args.bucket, key):
                        raise transfer.TransferError(
                            "unknown", "uploaded but not found at %s" % key)

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
