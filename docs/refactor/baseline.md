<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the measurements taken before the legibility refactor starts, to be compared again after every stage -->

# Refactor baseline, 24 November 2026

What `refactor/legibility` measured before any code moved. Each item is
measured again after every stage and must come out the same, except
complexity, which is expected to fall.

- Branch: `refactor/legibility`, created from `docs/readme-sections` at `1fc4b6a`.
- Code under measurement is that of `main` at `c9754dd` (v0.21.0): the docs branch changed docstrings and comments only.
- Machine: Windows 11, Python 3.12 in `.venv`, KCL VPN connected.

## 1. Tests

- `.venv\Scripts\python -m pytest -q`: **1011 passed, 1 skipped**, 21 subtests passed.
- `python -m unittest` (system Python, no web extras): **1012 run, OK, 426 skipped**.

Both counts must stay exactly as they are.

## 2. Command-line dry run

Compared back to back, not against a saved file:

```
python docs/refactor/baseline/compare_dry_run.py
```

- The script unpacks `main` into a temporary folder, runs the same dry run there and in this checkout seconds apart, and prints `dry run: same` or the lines that differ. On 24 November: **same** (exit 0 and 59 lines on both sides).
- Why back to back: a dry run needs the VPN, lists the store to see whether the project and dataset exist, and prints the subject list from the vocabulary as fetched. A saved output would go stale when the store or the vocabulary changed; two runs seconds apart see the same store and vocabulary.
- No output is saved in the repository. Its first line holds a username and a local path.
- The inputs are in `baseline/`: `sample/` (two small files, one in a sub-folder), `prov.json` (an empty list, which skips the origin questions) and `dry-run-answers.txt` (the answers to the remaining questions, one per line). The flags are `--strand rs2 --project csac --dataset baseline-check --state 2_final --sensitivity green --domain quant --provenance ... --dry-run`.

## 3. Web service routes

Built with the test settings against a mocked store (no network), by `baseline/collect.py`.

- [baseline/routes-openapi.txt](baseline/routes-openapi.txt): every method and path in the OpenAPI document, sorted. **22 routes.**
- [baseline/routes-registered.txt](baseline/routes-registered.txt): every route in the order the app registers it, including the static mount and the documentation pages. **24 lines.** This is the one to watch when routes are split into routers, because order decides which route answers when two could match.

The 22 routes:

- `GET /`, `/health`, `/whoami`, `/auth/headers`, `/connectivity`, `/privacy`, `/vocabulary`
- `POST /deposits`, `GET /deposits/lookup`, `GET /deposits/{deposit_id}`, `GET /deposits/{deposit_id}/summary`
- `PUT` and `DELETE /deposits/{deposit_id}/files/{member}`, `POST /deposits/{deposit_id}/finalise`
- `GET /datasets`, `/datasets.json`, `/datasets/{identifier}`, `/datasets/{identifier}/record`, `/datasets/{identifier}/files/{member}`
- `GET /search`, `/search.json`, `/search/why`

Both lists were taken twice: with the test settings (read role, search and the LLM platform off), and with the read role, the LLM platform (and so search) and the `/auth/headers` diagnostic switched on using dummy values. The second pair is `routes-openapi-switches-on.txt` and `routes-registered-switches-on.txt`. **The two pairs are identical**: every route is registered whatever the switches say, and a switch changes what a route answers, not whether it exists.

Limits: the passages switch (`CRSW_PASSAGES`) was left off, because it needs a DuckDB file on disk. The lists show which routes exist, not what each returns; the tests cover that.

## 4. Promoter help text

[baseline/promoter-help.txt](baseline/promoter-help.txt): `python -m promoter --help` and the help of each of the seven subcommands (`run`, `recategorise`, `audit`, `datasets`, `index`, `passages`, `remove`), at a fixed width of 100 columns. 105 lines. Must match exactly.

## 5. Public names

[baseline/public-names.txt](baseline/public-names.txt): every name not starting with an underscore in `crsw_deposit` and its eleven modules, and in `crsw_web.bulk`, with its kind (function, class, constant). 236 lines. `cdisaw-parquet` imports from both, so no name may disappear or change kind. New names may appear.

## 6. Complexity

[baseline/complexity.txt](baseline/complexity.txt): ruff 0.17.6, rule C901, run from outside the project with `uvx ruff check --isolated --select C901 --output-format concise`. Nothing was added to `pyproject.toml`. **41 functions score over 10.**

The ones the stages name:

- 193 `crsw_web/app.py` `create_app` (Stage C)
- 63 `deposit.py` `prompt_metadata` (Stage D)
- 58 `crsw_deposit/record.py` `validate_record` (Stage A)
- 45 `crsw_deposit/authority.py` `validate_authority` (Stage A)
- 45 `crsw_web/metadata.py` `validate_meta` (Stage C)
- 45 `promoter/checks.py` `check` (Stage B)
- 38 `crsw_web/app.py` `search_all` (Stage C)
- 38 `deposit.py` `main` (Stage D)

File sizes for the same stages: `deposit.py` 1,839 lines, `crsw_web/app.py` 1,282, `crsw_deposit/record.py` 1,123, `promoter/__main__.py` 859.

## How to measure again

```
.venv\Scripts\python -m pytest -q
python -m unittest
.venv\Scripts\python docs\refactor\baseline\collect.py SOME_OTHER_FOLDER
python docs\refactor\baseline\compare_dry_run.py
python docs\refactor\compare_validators.py
```

The last feeds the same damaged records and vocabularies to `validate_record` and `validate_authority` on `main` and on this checkout, and reports any case where the answers differ. The third writes the routes, help text and public names to another folder; compare each file with its baseline. The fourth is the dry-run comparison. The ruff command is as given above.
