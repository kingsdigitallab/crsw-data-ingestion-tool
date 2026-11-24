<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: things noticed during the legibility refactor that are not fixed on the refactor branch -->

# Findings, not fixed on this branch

Anything noticed while making the code easier to read that would change
behaviour, or belongs to someone else's decision, is written here and
left alone.

## Known before the work started

- `scripts/check-no-secrets.sh` fails on a Windows checkout because of CRLF line endings; it passes with carriage returns stripped. Likely remedy: a `.gitattributes` rule forcing LF for shell scripts, as its own change on `main`.
- Ruff's B904 (reported in the handoff as 42 places where `raise` inside `except` has no `from`) and its unused-variable findings, for GN to judge. Not re-measured here.

## Already corrected on `docs/readme-sections`

- `docs/dataset-record.md` said the schema was 0.6; the code is on 0.7. Corrected in `b0017d2`.
- `docs/cli-guide.md` said the specs ran to `_R9`; r13 exists. Corrected in `b0017d2`.

## Noticed while taking the baseline (24 November 2026)

- `python deposit.py --help` lists `centre` among the choices for `--strand`. `CLAUDE.md` says the command line offers `rs1` to `rs4` only. Not checked whether a later step refuses it.
- `pyproject.toml` gives the project version as `0.6.0`; `crsw_deposit.__version__` is `0.7.0` (it tracks the record schema) and the release is v0.21.0. Three numbers for three things, but the first looks left behind.
- The `--dry-run` parity check depends on the live store and the fetched vocabulary, so its output can change with no change to the code. On this branch it is therefore compared with `main` back to back (`baseline.md`, section 2).
