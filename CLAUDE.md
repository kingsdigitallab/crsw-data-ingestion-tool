# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A command-line tool for depositing research files into CRSW's shared object storage (Ceph, S3-compatible, via rclone), applying path conventions and writing a sidecar metadata JSON per object. The tool is built and working. `DEPOSIT_TOOL_SPEC.md` is the original brief; `DEPOSIT_TOOL_SPEC_r2.md` revises it after first live testing and **supersedes it where it speaks** — read both before changing behaviour.

## Hard constraints (violating any of these is a rewrite)

- **Python stdlib only.** No pip, no venv, no requirements.txt. This is why the vocabulary is JSON, not YAML.
- **Python 3.8 floor.** No `dict | dict`, no runtime `list[str]` annotations (use `typing.List` or skip), no `str.removeprefix`.
- **Cross-platform.** `pathlib` for local paths, but object keys always use `/` — never let `os.sep` leak into a key.
- **PyInstaller-compatible.** No dynamic imports; use a `sys._MEIPASS` fallback when locating the bundled `vocab.json`.
- **No secrets.** Credentials live in rclone's config. Never write, log, or echo it.

## Architecture: the separation that matters

This tool is a prototype of a future web ingestion gateway's back end. The gateway will import the logic modules and put a web form in front — so validation, field logic, and key construction must be importable with **no interactive I/O**:

- `deposit.py` — CLI only: argparse, prompting, preview, confirmation, progress, error translation. The only file allowed to talk to the user.
- `sidecar.py` — field definitions, validation, JSON assembly, checksum. No user I/O, pure functions where possible. This is what the gateway imports.
- `keys.py` — path construction + filename checks. Kept separate from `sidecar.py` because they change for different reasons (field list vs. path convention).
- `vocab.json` — bundled fallback copy of the subjects vocabulary. Runtime fetch from GitHub → cache (`%LOCALAPPDATA%\crsw-deposit\` / `~/.cache/crsw-deposit/`) → bundled copy, in that order.

If it's written as one interactive blob it gets thrown away.

## Domain rules that are deliberate decisions (don't "fix" them)

- **Filenames are preserved exactly as deposited** (handbook v0.4 §4.3 reversed an earlier renaming convention). Offer corrections for bad characters; never auto-apply, never rename silently.
- **`sensitivity=red` is refused outright** — red data never enters shared storage; point at the TRE (handbook §3, §7.3).
- **Unknown subject terms are refused** with instructions for proposing an addition — not silently accepted or dropped.
- **`rclone copyto`, never `rclone copy`** — `copy` treats the destination as a directory and keeps the original name inside it. This has already caused a real incident.
- **Data file uploads first, sidecar second.** An object briefly without a sidecar is recoverable; a sidecar pointing at nothing is confusing.
- **Batches are not atomic** and re-runs must be safe (deterministic keys mean re-run overwrites; bucket versioning makes that recoverable — say so, don't do it silently).
- Key layout: `{strand}/{project}/{state}/{sensitivity}/{filename}`; sidecar key is always `{data_key}.meta.json`. Controlled values for strand/state/sensitivity are in spec §4; the sidecar schema is v0.3 (r2: `source_type`/`source_detail` replace `source`; version stored as `3-0`, no `v`).
- **Domains are fetched from the vocabulary, not hardcoded** (r2 §2) — `keys.DOMAINS` is only the fallback for stale caches. Choosing a domain auto-fills its steward (unless "TBC").
- **Upload verification compares sizes, never checksums-vs-ETags** (r2 §0) — multipart ETags are hash-of-hashes and will not match a plain SHA-256.
- Remote/bucket resolution: command-line flag → `config.json` (`%LOCALAPPDATA%\crsw-deposit\` or `~/.config/crsw-deposit/`) → built-in defaults. `--reconfigure` re-runs setup.
- All ANSI colour goes through `deposit.style()`; colour is never the only signal, and `NO_COLOR`/`FORCE_COLOR`/non-tty are honoured.

## Error messages are the product

Preflight failures (§8) each need a specific, actionable message: VPN-off must name the KCL VPN, not print a socket error; missing rclone must give the download URL and config stanza; permission failures must explain strand scoping. Spec's test: turn the VPN off and run it — if the output mentions sockets, TLS, or S3 error codes, it isn't finished. The environment-failure test cases in §10 matter more than the happy path.

## Commands

No build system or test framework exists yet. Given the stdlib-only constraint, tests should use `unittest` (`python -m unittest`). The tool itself runs as:

```
python deposit.py FILE_OR_GLOB [...] [--strand rs2 --project csac --state 2_final --sensitivity green --dry-run --remote ceph --bucket crsw]
```

`--dry-run` with full flags is the reproduction path for support — keep it working.
