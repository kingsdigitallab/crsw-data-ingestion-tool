# CRSW Deposit Tool — revision spec (r7)

Folders become a first-class deposit argument; `--remote`/`--bucket` can
persist to config with a confirmation; cross-platform behaviour is
verified rather than assumed.

**Supersedes:** base spec §7 step 2 ("Expand globs. Reject directories
— no `--recursive` in v1"). Extends r6 §1 (dataset as a path element)
and r6 §2 (reserved record pattern) one level further, into the member
path. Everything else in r2–r6 stands.
**Origin:** a working session on three requests — depositing a whole
folder in one command, saving `--remote`/`--bucket` choices instead of
re-typing them, and confirming the tool actually works on macOS/Linux
rather than assuming it from source. The last one surfaced two genuine
defects (§4, §7) that predate this revision and are fixed here because
documenting them without fixing them would mean shipping a README that
says records deposited from a Mac may silently duplicate.
**Dependency:** unchanged — partner endorsement of the r6 dataset-as-unit
model is still outstanding. Build; don't roll out.

---

## 1. Folders as deposit arguments, structure preserved

```
was:  reject directories outright ("no --recursive yet")
now:  walk the folder; each file's path relative to the argument's
      root becomes part of both the object key and the manifest path

python deposit.py survey-2024/ --dataset coastal ...
  survey-2024/2024/tiles/a.tif  ->  .../coastal/2024/tiles/a.tif
  survey-2024/readme.md         ->  .../coastal/readme.md
```

**The anchoring rule:** every argument carries its own root. A bare
folder's root is the folder itself (its own name is never part of the
key — the dataset element already names the collection). An explicit
file's root is its parent, unchanged from before r7. A glob's root is
the literal directory prefix before its first wildcard component, so
`data/*/results.csv` anchors at `data` and `data/site1/results.csv` /
`data/site2/results.csv` no longer collide the way two flat same-named
files would. A trailing slash makes no difference either way — shells
add one inconsistently via tab-completion, so it is not treated as a
signal.

`**` now recurses to any depth. It did not before: `glob.glob()` without
`recursive=True` silently collapses `**` to exactly one level, with no
warning — confirmed against Python's own `glob` module before this was
written. Any existing use of `**` now covers more files than it did.

A real filename containing `[` or `]` (e.g. `data[1].csv`) is matched
literally, with a note — previously reported as "no file matches"
because the brackets are read as a wildcard character class.

Object-key construction moves from `keys.build_key(..., filename)` to
`keys.build_key(..., member)`, where `member` may contain `/`. Structural
validity (no `..`, no absolute or drive-rooted paths, no backslash, no
control characters, length limits) is enforced in `build_key` itself —
the one place the "never let `os.sep` leak into a key" rule can be
guaranteed for every caller, including the future gateway, which never
imports `deposit.py`. Advisory, per-segment problems (the existing
spec §4 character set) are offered as a correction the same way a flat
filename's problems always were.

## 2. Inclusion policy: nothing silent

| Case | Policy |
|---|---|
| OS/editor noise (`.DS_Store`, `Thumbs.db`, `._*`, `.git/`, `__pycache__/`, `__MACOSX/`, …) | excluded by default, count reported; `--include-noise` to keep |
| Hidden/dotfiles | included (unlike a bare `*` glob, which skips them) — count reported, since the difference is otherwise invisible |
| Symlinked file | followed |
| Symlinked directory | not descended (cycle risk) — named |
| Broken symlink | reported as a problem |
| Unreadable subdirectory | reported as a problem, never silently skipped |
| Empty directory | contributes nothing (object storage has no directories) |

The walk uses `os.walk`, not `Path.rglob`: `rglob` swallows `OSError` on
an unreadable subdirectory, cannot be pruned before descending, yields
filesystem order (so the manifest would differ between NTFS/APFS/ext4),
and follows symlinked directories by default. Every directory level is
sorted before use, so the walk — and therefore the manifest — is
deterministic across platforms and re-runs.

## 3. Reservation applies at every segment

r6 §2 reserves the `dataset.*.json` **pattern**, not one fixed name, for
member filenames. With member paths now able to nest, the reservation
applies to **every segment of a member path, not just the last**:
`sub/dataset.foo.json` is refused exactly like `dataset.foo.json`. A
record nested at any depth still looks, out of context, like the record
of a dataset called `sub` — the whole reason the pattern exists.
`record.py`'s manifest-path validation and `dataset.schema.json`'s
`files[].path` pattern both move from an anchored
`^dataset\..*\.json$` to a segment-aware `(^|/)dataset\..*\.json$`
accordingly; missing this in either place would let a nested reserved
name back in silently.

## 4. Unicode normalisation (NFC)

macOS hands back decomposed (NFD) Unicode for accented filenames; Windows
and Linux hand back composed (NFC). `sha256_file` hashes content, so the
checksum for the same file is identical either way — but the *path*
string differs, and the manifest's unchanged/merge logic
(`record.unchanged_paths`, `record.merge_manifest`) compares `path` as a
plain string. Confirmed: this is a **defect that predates r7**, not
something folder deposits introduce, though a folder walk multiplies the
exposure (every directory segment is now also a normalisation risk).

**Resolution:** every member path is NFC-normalised at the point it is
built from the filesystem (`keys.normalise_member_path`), and the
normalised string is used for the object key, the manifest path, and
every comparison. The local file on disk is never renamed or
re-derived from the normalised string — only read from the `Path` the
OS gave, since opening the NFC name can fail on a strict-NFD volume.
The tool says so when normalisation actually changes a string; this is
encoding normalisation, not renaming, so it does not contradict "filenames
are preserved exactly as deposited" (handbook v0.4 §4.3).

**Explicitly not done:** reconciling *existing* NFD records by
normalising both sides inside `merge_manifest`. That would rewrite a
legacy entry's `path` to NFC while the object still sits at the NFD key,
and the completion check (`prefix + "/" + entry["path"]`) would then look
for the wrong key and fail the whole deposit at the last step. A batch
whose checksum matches an existing entry at a different path — the
NFD/NFC case, and also the "previously flat, now inside a folder" case —
gets a preview warning instead (§5). Reconciling legacy members stays
manual curation, consistent with "the tool never removes a manifest
member."

## 5. Additive re-deposit, made visible

A file re-deposited under a different path from before — most likely
because it was flat and is now inside a folder, or because of §4's
NFD/NFC case — is a *new* manifest entry, not an update, because deposits
are additive and the tool never removes a member. Left unannounced this
silently doubles the bytes in a dataset. The preview now warns, before
the point of no return, whenever a batch entry's checksum matches an
existing entry at a different path. It warns, never blocks: two genuine
copies of a file in two folders is legitimate.

## 6. `--remote`/`--bucket` persistence, with confirmation

Previously transient: a flag applied to one run only, and `save_config`
had exactly one caller (`first_run_setup`). Now, once a flag's value has
been proven reachable and writable and differs from what's saved, the
tool asks once — `Save ceph:crsw as your default target? [y/N]` — and
writes it on yes. The offer is skipped entirely when: `first_run_setup`
or `--reconfigure` already saved this run's values (no double prompt);
neither flag was actually given; `--dry-run` is set (the non-interactive
support-reproduction path must never mutate the machine); stdin is not a
tty (scripted/piped runs); or the proposed values already match what's
saved. This also closes a pre-existing gap: a user with no saved config
who supplies *both* flags on the command line was previously never
asked and ran forever with no config.

## 7. Cross-platform fixes

Confirmed working, unchanged, on Windows and (via WSL Ubuntu 22.04) real
Linux: the full test suite, and a live folder deposit producing identical
member paths on both. macOS was not directly exercised — this stays code
inspection plus the tests below; worth one real run before rollout.

Two defects fixed, both pre-dating r7:

- **The uploaded record's newline style depended on the depositor's OS**
  (`Path.write_text` applies platform newline translation — CRLF on
  Windows, LF on macOS/Linux), so the record's own
  `x-amz-meta-checksum-sha256` label differed by platform for
  byte-identical JSON, and bucket versioning recorded a spurious change
  every time the team alternated OS. Every write of the record, the
  saved config, and the deposit log now forces `newline="\n"`.
- **`find_rclone` only searched the current working directory**, not the
  script's own directory as `MESSAGES["no_rclone"]` already promised —
  invisible on Windows (where they're usually the same) but wrong for
  the common POSIX idiom of running the script from a data directory
  elsewhere. It now also checks the script directory (with a
  `sys._MEIPASS` fallback for a frozen build), and requires the
  candidate to be executable, so a downloaded-but-not-`chmod`ed or
  Gatekeeper-quarantined rclone is skipped rather than picked and then
  failing with a bare `OSError` traceback — which the spec's own error-
  message test (§8 elsewhere in this document) rules out.

## 8. Schema and contract changes

`dataset.schema.json`, `files[].path` (schema 0.5, no version bump —
additive): the reserved-name pattern becomes segment-aware (§3);
`minLength`/`maxLength` and a no-leading/trailing-slash pattern are
added, mirroring the structural (Tier 1) checks `keys.member_path_error`
already enforces at runtime. A flat legacy `path` value still validates
unchanged. `crsw-dc-mapping.json`'s note on `path` — "relative to dataset
prefix" — is now load-bearing rather than descriptive: it is what makes
`dcat:downloadURL` resolvable as `{prefix}/{path}` for a nested member.

## 9. Touch points

`keys.py` (new: `normalise_member_path`, `member_path_error`,
`member_path_problems`, `suggest_member_path`, `is_reserved_member`;
changed: `build_key`'s last parameter). `record.py` (reserved-path and
structural checks in `validate_record`, both segment-aware). `deposit.py`
(`Source` namedtuple, `resolve_files`, `plan_deposits`, `set_member`,
`prepare_entries`, `preview_lines`, `confirm_filenames`,
`prompt_per_file_overrides`, `duplicate_content_warnings`,
`offer_to_save_settings`, `interactive`, the record/config/log writers).
`transfer.py` (`find_rclone`, `_run`). `dataset.schema.json`.

## 10. Carried over, not built here

r6 §8's "manifest split at scale" — the `files` array is "comfortable at
forty entries and not at RS3's thousands of raster tiles" — was deferred
until RS3's real shape is known. A single folder deposit now reaches that
shape in one command rather than thousands of manual invocations, which
makes the deferred problem easy to trigger by accident. Not built here;
worth reopening once real folder-shaped deposits are observed.
