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
- **Closed, 24 November 2026 (Neil checked `main`).** `record.mint_uuid`'s docstring said "Promotion or reclassification creates a NEW record with a new UUID; derived_from carries the lineage." "Promotion" there meant a change of state or sensitivity, not the promoter's move from staging to the store: a different state or sensitivity is a new prefix, so it gets a new record with a new UUID. The sentence is kept and reworded: "A dataset deposited at a different state or sensitivity is a new record with a new UUID; derived_from carries the lineage."
- **Corrected in the docstring, 24 November 2026.** `deposit_logic.dataset_uuid_for`'s docstring on `main` says the UUID is "minted once per prefix, stable across moves". "Stable across moves" is wrong: a dataset does not move between prefixes, and a deposit at another state or sensitivity gets a new UUID. The docstring now says the UUID is kept for re-deposits to the same prefix and through staging. `docs/decisions/deposit_logic.md` repeated the phrase and is corrected too. The code was right throughout; `main` still carries the old wording.
- Several small functions in `crsw_deposit/` have no docstring at all (for example `places.codes`, `places.labels_of`, `vocab.cache_dir`, `vocab.domain_codes`, `authority.is_authority`, `record.utc_now_iso`, the `cmd_*` handlers in `vocab_cli.py`). The sweep rewrote existing docstrings and did not add new ones.

## Noticed during Stage B, `promoter/` (24 November 2026)

- `promoter/__main__.py` still imports `vocab`, `s3mod` and `recat` although nothing in it uses them. The tests patch through that module (`promoter.__main__.vocab.load_vocabulary`, `promoter.__main__.s3mod.make_client`, `promoter.__main__.recat.plan`), and the tests are not to be edited. Pointing those patches at `crsw_deposit.vocab`, `crsw_web.s3` and `promoter.recategorise` would let the three imports go.
- The command modules import each other's helpers by their private names (`from .cmd_common import _setup`, and so on). The names were kept because a move must not also edit. Dropping the underscore from the shared ones would be a rename for a later commit.
- `dataclasses.field` is imported and unused in `promoter/config.py` and `promoter/passages.py` (ruff F401). Both were there before this branch.
- `docs/web-deposit/remove-from-store-handoff.md`, `embedding-model-fingerprint-handoff.md` and `passage-length-handoff.md` say that `cmd_remove` and the embedding call sites are in `promoter/__main__.py`. They are now in `promoter/cmd_remove.py` and `promoter/cmd_derived.py`. Those documents are records of past work and were left as written.
- Done since: `check()`'s docstring and several comments in `promoter/` cited spec revisions (r8 §4, r9, r11, r13). The docstring sweep of `promoter/` removed them; the reasons are in `docs/decisions/promoter/`.

## Noticed while taking the baseline (24 November 2026)

- `python deposit.py --help` lists `centre` among the choices for `--strand`. `CLAUDE.md` says the command line offers `rs1` to `rs4` only. Not checked whether a later step refuses it.
- `pyproject.toml` gives the project version as `0.6.0`; `crsw_deposit.__version__` is `0.7.0` (it tracks the record schema) and the release is v0.21.0. Three numbers for three things, but the first looks left behind.
- The `--dry-run` parity check depends on the live store and the fetched vocabulary, so its output can change with no change to the code. On this branch it is therefore compared with `main` back to back (`baseline.md`, section 2).
