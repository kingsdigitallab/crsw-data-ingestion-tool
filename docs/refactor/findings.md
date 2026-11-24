<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: things noticed during the legibility refactor that are not fixed on the refactor branch -->

# Findings, not fixed on this branch

Anything noticed while making the code easier to read that would change
behaviour, or belongs to someone else's decision, is written here and
left alone.

## Known before the work started

- `scripts/check-no-secrets.sh` fails on a Windows checkout because of CRLF line endings; it passes with carriage returns stripped. Likely remedy: a `.gitattributes` rule forcing LF for shell scripts, as its own change on `main`.
- Ruff's B904 (reported in the handoff as 42 places where `raise` inside `except` has no `from`) and its unused-variable findings, for GN to judge. Not re-measured here.

- The repository is public on GitHub, and it contains the operations runbook (`docs/web-deposit/runbook.md`) and the security review (`docs/web-deposit/security-review.md`). Whether those two belong in a public repository is a decision for Neil and GN. Removing a file now would not remove it from the history.

## Already corrected on `docs/readme-sections`

- `docs/dataset-record.md` said the schema was 0.6; the code is on 0.7. Corrected in `b0017d2`.
- `docs/cli-guide.md` said the specs ran to `_R9`; r13 exists. Corrected in `b0017d2`.

## Noticed during the docstring sweep of `crsw_deposit/` (24 November 2026)

- The module docstring of `record.py` said "dataset record v0.4" and named the file `dataset.meta.json`; `RecordParseError`'s said the same name. Both were years out of date. The new docstrings give the current name. No code was involved.
- `record.mint_uuid`'s docstring said "Promotion or reclassification creates a NEW record with a new UUID; derived_from carries the lineage." The promoter keeps the UUID when it merges into an existing record and when categories change, so the sentence was dropped. Worth a check by someone who knows whether any route still mints a second UUID for a dataset that moves between states.
- Several small functions in `crsw_deposit/` have no docstring at all (for example `places.codes`, `places.labels_of`, `vocab.cache_dir`, `vocab.domain_codes`, `authority.is_authority`, `record.utc_now_iso`, the `cmd_*` handlers in `vocab_cli.py`). The sweep rewrote existing docstrings and did not add new ones.

## Noticed while taking the baseline (24 November 2026)

- `python deposit.py --help` lists `centre` among the choices for `--strand`. `CLAUDE.md` says the command line offers `rs1` to `rs4` only. Not checked whether a later step refuses it.
- `pyproject.toml` gives the project version as `0.6.0`; `crsw_deposit.__version__` is `0.7.0` (it tracks the record schema) and the release is v0.21.0. Three numbers for three things, but the first looks left behind.
- The `--dry-run` parity check depends on the live store and the fetched vocabulary, so its output can change with no change to the code. On this branch it is therefore compared with `main` back to back (`baseline.md`, section 2).
