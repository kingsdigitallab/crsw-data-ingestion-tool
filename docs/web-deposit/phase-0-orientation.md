# Phase 0: orientation on the existing deposit tool

Date: 18 September 2026
Status: checkpoint passed 18 September 2026. Section 4 records the agreed layout; section 6 records the answers.

Source read: `crsw-data-ingestion-tool` (GitHub kingsdigitallab, HEAD `91257c1`), its `CLAUDE.md`, specs R5 to R7, `keys.py`, `record.py`, `vocab.py`, `deposit.py`, `transfer.py`, `dataset.schema.json`, and the tests. All 335 tests pass on this laptop.

## 1. Summary

The tool was written with this gateway in mind. `keys.py`, `record.py` and `vocab.py` already contain no user I/O and hold nearly all of the convention logic. They can be imported today with one mechanical change (flat `import keys` becomes a relative import once they sit inside a package). Two pieces of convention logic still live in `deposit.py` and must be lifted: the assembly of the four object labels, and the orchestration that turns metadata plus manifest entries into a validated record. `transfer.py` is rclone-specific and stays with the CLI.

## 2. Function inventory by responsibility

### Key construction: `keys.py` (imports cleanly, no I/O)

| Function | Role |
|---|---|
| `build_key(strand, project, sensitivity, state, dataset, member)` | The object key. Validates every part, refuses red, refuses reserved names at any depth. The one place the "never let `os.sep` leak" invariant is enforced. |
| `dataset_prefix(...)` | Five-part prefix, also the record's `identifier`. |
| `record_key(...)`, `record_filename(dataset)` | `{prefix}/dataset.<dataset>.json`. Deterministic, no listing needed. |
| `normalise_member_path(parts)` | Joins segments with `/`, NFC-normalised. Needed by the service because browsers on macOS also hand back decomposed Unicode. |
| `member_path_error(member)` | Structural refusal (backslash, `..`, control chars, length). |
| `member_path_problems(member)`, `suggest_member_path(member)` | Advisory per-segment problems and the never-auto-applied suggestion. |
| `is_reserved_member(member)`, `is_reserved_name(name)` | `dataset.*.json` pattern at any segment. |
| `filename_problems`, `suggest_filename` | The spec §4 character set. |
| `normalise_project`, `validate_project`, `similar_projects` | Slug rules shared by project and dataset names. |
| `RedDataError`, `STRANDS`, `STATES`, `SENSITIVITIES`, `DOMAINS`, `PROBLEM_CHARS` | Constants and the red backstop. |

### Metadata validation and record assembly: `record.py` (imports cleanly, no I/O)

| Function | Role |
|---|---|
| `build_record(...)` | Assembles the v0.5 record in field order. Omits empty optionals. |
| `validate_record(rec, vocab_terms, domain_codes)` | Returns `(errors, warnings)`. Runtime twin of `dataset.schema.json`. |
| `coverage_error`, `abstract_warning`, `normalise_version`, `unknown_subjects` | Field-level checks the form will call per field for inline feedback. |
| `manifest_entry(path, sha256, bytes, temporal, fmt, ...)` | One `files[]` entry. |
| `merge_manifest(existing, new)` | Union, additive only. Returns `(union, added, changed)`. |
| `unchanged_paths(existing, new)` | Members a re-run can skip. |
| `temporal_object`, `temporal_pair`, `widen`, `envelope_errors` | Coverage envelope logic. |
| `append_depositor`, `mint_uuid`, `utc_now_iso`, `guess_format` | Small helpers. `guess_format` deliberately uses a private MIME table so results do not vary by platform. |
| `record_json(rec)`, `parse_record(text)` | Serialise with `indent=2`, `ensure_ascii=False`, trailing newline. Parse refuses unknown schema versions and empty manifests. |
| `sha256_file(path)` | Chunked file hash. The service will need a streaming equivalent that hashes a request body as it passes through, not a file. |
| `default_depositor()` | Uses `getpass.getuser()`. The service must never call this; it passes the authenticated user explicitly. |

### Vocabulary: `vocab.py` (imports cleanly, no user I/O)

`load_vocabulary()` tries GitHub, then a cache dir, then the bundled `vocab.json`, returning `(dict, source)`. `all_terms`, `domains`, `domain_codes` read it. Two service considerations:

- `cache_dir()` picks `%LOCALAPPDATA%` or `~/.cache`. Fine in a container, but the service should pass an explicit cache path.
- `bundled_vocab_path()` uses `sys._MEIPASS` for PyInstaller. Once `vocab.json` is package data this needs `importlib.resources` with the `_MEIPASS` fallback kept for the CLI build.
- `VOCAB_URL` points at `crsw-kcl/crsw-vocabulary`, which the comment says is still to be created. Every run currently falls through to cache or bundled.

### Convention logic currently in `deposit.py` that must be lifted

| Location | What it does | Why it must move |
|---|---|---|
| `perform_deposits` lines 1307 to 1334 | Builds `base_labels` (`x-amz-meta-dataset-uuid`, `-sensitivity`, `-depositor`) and adds `-checksum-sha256` per object, including for the record itself. | The service sets the same four labels via boto3 `Metadata`. If the names or the rule "record gets its own checksum label" live only in the CLI, they drift. |
| `perform_deposits` lines 1348 to 1380 | Record orchestration: reuse existing UUID or mint; merge manifest; widen the coverage envelope over union plus the old envelope; `build_record` with `created` preserved from the existing record; `append_depositor`; `validate_record`. | This is the "what a deposit means" logic. The service's finalise endpoint must run exactly this. |
| Lines 1391 to 1396 | Writes the record with `newline="\n"` so the checksum label is identical across platforms. | Service must serialise to bytes the same way. |
| `plan_deposits`, `set_member` | Pairs `member` and `key` and refuses two sources landing at the same key. | The form needs the same collision check. |
| `prepare_entries` | Builds manifest entries from local files. | Service builds entries from stream results instead. Only the shape matters, which `record.manifest_entry` already owns. |
| `resolve_files` and the `NOISE_*` constants | Walks folders, drops OS noise, reports counts. | See open question 1. Browser file pickers can also supply `.DS_Store`. |
| Completion check (after line 1420) | Every manifest entry exists at `prefix + "/" + path` at the recorded size. Sizes only, never ETag. | The promoter runs this check. It belongs in the package. |

### Stays with the CLI

`transfer.py` (rclone subprocess wrapper, preflight probes, `--ignore-times`, header upload), all prompting and picker code, config.json handling, colour and tty logic, `deposits.log` appending.

## 3. Importability findings

- **Imports are flat.** `record.py` and `vocab.py` do `import keys`. Inside a package these become `from . import keys`. Tests do `import record` and will need `from crsw_deposit import record` or a `conftest`-style path shim. Straightforward but touches every test file.
- **No `print`, `input`, `sys.exit` or tty checks** in `keys.py`, `record.py` or `vocab.py`. Confirmed by reading; `vocab.py` imports `sys` only for `_MEIPASS`.
- **Environment-dependent defaults**: `record.default_depositor` (getpass) and `vocab.cache_dir` (LOCALAPPDATA). Both are fine as CLI defaults if the service always passes explicit values.
- **Python 3.8 compatible** throughout: `typing.List` etc., no walrus, no `removeprefix`. `datetime.date.fromisoformat` is 3.7+.
- **No third-party imports** anywhere in the three modules.
- **Local-only docs.** The sibling repo's `.gitignore` excludes `CLAUDE.md` and all `DEPOSIT_TOOL_SPEC*.md`. They exist only on this laptop. The package's public contract therefore needs to be captured in code and docstrings, or those files need to be committed, before another machine can build against it.
- **No git tags** exist yet. Pinning from this repo needs one.

## 4. Package layout (decided at the checkpoint: monorepo)

Both routes live in this repository and import one package. This removes version drift: there is no pin to forget, and one test run covers both.

```
crsw_deposit/                 # importable conventions, stdlib-only, Python 3.8+
  __init__.py                 # __version__, re-exports
  keys.py                     # moved as-is
  record.py                   # moved; relative import of keys
  vocab.py                    # moved; bundled path via importlib.resources with _MEIPASS fallback
  vocab.json                  # package data
  labels.py                   # NEW: object label names and assembly, lifted from deposit.py
  noise.py                    # NEW: OS-noise file/dir rules, lifted from deposit.py
  deposit_logic.py            # NEW: plan_keys, assemble_record, record_bytes, completion_problems
crsw_web/                     # FastAPI service, Python 3.12, boto3 (Phase 1 skeleton onward)
promoter/                     # Phase 4
deposit.py                    # CLI, unchanged behaviour; imports crsw_deposit.*
transfer.py                   # CLI-only rclone wrapper
deploy/                       # Dockerfile, nginx.conf, compose.yaml
docs/specs/                   # DEPOSIT_TOOL_SPEC*.md, now tracked
docs/web-deposit/             # this note, the plan, the proposal
pyproject.toml                # crsw_deposit has no runtime deps; extras [web] and [dev]
```

Notes:

- `python deposit.py` from a checkout keeps working because `crsw_deposit/` sits next to it. No install step for CLI users.
- `labels.object_labels` returns names without the `x-amz-meta-` prefix. rclone needs the prefix in its header flag and boto3 adds it itself, so each transport applies its own rule; the names live in one place.
- `deposit_logic.assemble_record` takes `now` as a parameter so tests are deterministic and the service stamps request time.
- Staging keys are `staging/<user>/<deposit-id>/<final-prefix>/<member>` so promotion is a prefix strip and the record is byte-identical before and after.
- The former `crsw-data-ingest-form` repo is a local archive; its contents are here under `docs/web-deposit/`.

## 5. Refactor scope for Phase 1

Minimal set of changes to the sibling repo:

1. Create `crsw_deposit/` and `git mv` the three modules and `vocab.json` into it. Fix the two internal imports.
2. Add `labels.py` and `deposit_logic.py` by cutting the identified blocks out of `perform_deposits`, leaving calls in their place.
3. Add `pyproject.toml`, `__init__.py`, update test imports, add tests for the two new modules.
4. Tag `v0.5.0`.

Proof the CLI is unaffected:

- All 335 existing tests pass, plus the new ones.
- A fully flagged `--dry-run` produces identical output before and after (capture once before starting, diff after).
- One real deposit to `staging/_test/` shows identical record bytes and identical object labels to a pre-refactor deposit of the same files. This doubles as the first fixture for the service's parity tests.

Estimated effort: a day, most of it test import updates and the PyInstaller data path.

## 6. Checkpoint answers

Decided 18 September 2026: (1) yes, noise rules shared; (2) staging keys embed the final prefix; (3) depositor is the KCL username; (4) local-only docs committed under `docs/`; (5) existing-record defaults deferred to the egress backlog; (6) bundled vocabulary is acceptable for the PoC.

The questions as originally put:

1. **Noise filtering in the browser.** A folder drop in Chrome or Edge includes `.DS_Store` and `Thumbs.db`. Should the form apply the CLI's `NOISE_*` rules? If yes, the constants and `_is_noise_file` move into the package too. Recommendation: yes, with the same "excluded N, tick to include" reporting.
2. **Staging key layout.** The plan says `staging/<user>/<deposit-id>/<filename>`. Recommendation: `staging/<user>/<deposit-id>/<final-prefix>/<member>`, so the promoter's move is a prefix strip and the record inside staging already has the correct `identifier`. That keeps the record byte-identical between staging and destination.
3. **Depositor identity.** The CLI records the OS username. The service will record the SSO identity. Should the record store the KCL username (`k1078591`) so the two routes agree? Recommendation: yes.
4. **Commit the local-only docs?** `CLAUDE.md` and the R-specs are gitignored in the sibling repo. Once this service depends on that package from GitHub, the conventions need to be readable there. Recommendation: commit them, or fold the durable rules into a `docs/conventions.md` in the package.
5. **Existing-record lookup.** Repeat deposits load the existing record as defaults. The service's staging-only key cannot read the destination prefix. Options: the form does not offer defaults in the PoC, or a read-only listing key is added later alongside the egress work. Recommendation: defer; note in the egress backlog.
6. **Vocabulary source.** `VOCAB_URL` targets a repo that does not exist yet. The service will always fall through to the bundled copy. Acceptable for the PoC, but the package version then pins the vocabulary too.
