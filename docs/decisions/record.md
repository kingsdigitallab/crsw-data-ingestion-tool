<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in crsw_deposit/record.py, kept as a list -->

# Decisions behind `crsw_deposit/record.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r5 Q4" means question 4 of
`DEPOSIT_TOOL_SPEC_R5.md`, and "r8 §1" section 1 of `_R8.md`.

## The module

- **Decision:** one record per dataset prefix holds the descriptive fields and the files manifest; per-file sidecars are retired.
  **Reference:** r5 §1 Q2.
- **Decision:** no user I/O in this module.
  **Reason:** the web service imports it.
- **Correction made in passing:** the old module docstring said "dataset record v0.4" and named the file `dataset.meta.json`. The schema is 0.7 and the file is `dataset.<dataset>.json`. The new docstring says the latter and gives no version, which lives in `SCHEMA_VERSION`.

## Schema versions (`SCHEMA_VERSION`, `upgrade_record`, `parse_record`)

- **Decision:** every tool writes `SCHEMA_VERSION`; older accepted versions are read and upgraded on the way in.
  **Reason:** nobody converts a record by hand.
  **Reference:** r8 §5.
- **Decision:** 0.5 to 0.6 turns the string `derived_from` into one reference; `files[].derived_from` stays a string.
  **Reference:** r8 §5.
- **Decision:** 0.6 to 0.7 turns the string `domain` into a one-element list, moves a free-text `spatial` to `spatial_note`, and keeps a list of place codes as it is. `provider` and `spatial_note` are new.
  **Reference:** r11 §2 and §2.5.
- **Decision:** a current record is returned untouched by `upgrade_record`.
  **Reason:** the write-then-read-back checks rely on it.
- **Decision:** a record with a newer `schema_version` than the tool knows is refused.
  **Reason:** never overwrite the future blind.
- **Decision:** `RecordParseError` must be reported and the run stopped.
  **Reason:** modifying a record that was never read would silently drop its manifest members.
  **Reference:** r5 §4.
- **Decision:** whether a record is absent or could not be fetched is not this module's question.
  **Reason:** that distinction belongs to the transfer layer.

## Small rules

- **Decision:** an abstract under 50 words warns and never blocks; the guidance lives in the tool, not in the schema.
  **Reason:** a constraint in the contract that the tool does not enforce only breeds a permanently red CI test.
  **Reference:** r6 §0.
- **Decision:** a version is stored as `{major}-{minor}`; `3-0`, `v3-0`, `3.0` and `v3.0` are accepted and a bare `3` is rejected, never guessed.
  **Reference:** r2 §3.
- **Decision:** a dataset's UUID4 is minted once, at first record creation.
  **Reference:** r5 Q1.
- **Decision:** a change of state or sensitivity makes a new dataset with a new UUID, linked to the old one by `derived_from`.
  **Reason:** state and sensitivity are parts of the prefix, so the changed dataset lives at a new prefix with its own record. The old docstring called this "promotion or reclassification"; it was reworded on 24 November 2026 (Neil's ruling) so that it cannot be confused with the promoter, which moves a deposit from staging to the store and keeps the UUID.
  **Reference:** r5 Q1.
- **Decision:** a file's MIME type comes from a private table; an unknown type is omitted, never guessed.
  **Reason:** the default table consults the Windows registry, which maps `.csv` to `application/vnd.ms-excel`, so the manifest would differ by the depositor's platform.
  **Reference:** r5 §2.
- **Decision:** `sha256_file` reads in chunks.
  **Reason:** a 10 GB raster must never load into memory.

## Coverage

- **Decision:** temporal coverage is a nested `{"start", "end"}` object at record and manifest level.
  **Reason:** it is the DCAT-shaped form of `dcterms:temporal`.
  **Reference:** r6 §3.1.
- **Decision:** every per-file range must sit inside the record's envelope.
  **Reference:** r5 Q5.
- **Decision:** `temporal_pair` returns `(None, None)` for a malformed value and does not complain.
  **Reason:** it only needs not to crash on bad input; `validate_record` enforces the shape.

## Manifest (`manifest_entry`, `merge_manifest`, `unchanged_paths`)

- **Decision:** the manifest is the union of the existing one and the current batch; existing paths are never dropped.
  **Reason:** removing a member is deliberate curation, out of scope for the tool.
  **Reference:** r5 Q4.

## Derivation and provenance

- **Decision:** one rule turns what a person typed into a `derived_from` reference.
  **Reason:** the command line's interview, the web form and the 0.5 upgrade all use it, so the routes cannot drift.
  **Reference:** r8 §1 (decided).
- **Decision:** a reference is a dataset in the store or something outside it; an activity is one transformation step, its kinds managed in the vocabulary with a built-in fallback.
  **Reference:** r8 §1 and §2.
- **Decision:** an activity's outputs and bare-path inputs must be members of the dataset; a commit that is not 7 to 40 hex characters is a warning, nothing more.
  **Reference:** r8 §2.

## Providers and places (`provider_errors`, `place_errors`)

- **Decision:** with the list in hand an unknown provider slug or place code is an error; without it, a warning that it could not be checked.
  **Reason:** an old cache must never block a deposit.
  **Reference:** r11 §2.2 and §2.3; place codes r11 §4.

## Changes after deposit (`apply_dataset_change`, `apply_vocabulary_mapping`, `field_set_error`)

- **Decision:** a steward's decision about one dataset can add and remove subject terms, set the domain, and set descriptive fields in place; each is recorded in the record's own `category_history`.
  **Reference:** r9 §4; r11 §1.
- **Decision:** only descriptive fields can be set in place (`SETTABLE_FIELDS`).
  **Reason:** `subject` and `domain` have their own change keys and history kinds; `derived_from`, `provenance`, `depositors` and `category_history` accumulate through deposit and are never overwritten; every other required field is identity or the manifest and changes only through a new deposit. `abstract` is required, so it can be replaced but not cleared (added 5 November 2026 for the Centre's review of abstracts).
  **Reference:** r11.
- **Decision:** `field_set_error` is shared by the change-file reader and `apply_dataset_change`.
  **Reason:** a bad change file is refused before any record is read.
- **Decision:** a vocabulary change is applied by the actor "vocabulary", and a record whose terms are all current comes back as it was.
  **Reason:** it is safe to run on every record on every run; a rewrite that ends where it began would be repeated on every walk of the store.
  **Reference:** r9 §2 and §3.

## Building and validating (`build_record`, `validate_record`)

- **Decision:** `subject` and `license` carry the Dublin Core spellings.
  **Reference:** r6 §3.1.
- **Decision:** `validate_record` is written by hand.
  **Reason:** the shipped tool is standard-library only and cannot use `jsonschema`. `dataset.schema.json` is the contract for CI and other consumers; this function is the runtime check.
  **Reference:** r5 §7.
- **Decision:** domain and activity codes come from the fetched vocabulary, with the built-in lists as a fallback.
  **Reason:** fetched, not hardcoded.
  **Reference:** r2 §2.
- **Decision:** an identifier that does not match the record's path parts is an error, not a warning.
  **Reason:** the identifier is the five-part prefix and its last element must equal the dataset field; a mismatch means the record was moved or edited by hand.
  **Reference:** r6 §2.
- **Decision:** a bare string in `derived_from` or `domain` is an error in `validate_record`.
  **Reason:** it is an older shape arriving without going through `parse_record`, which would have upgraded it.
  **Reference:** r8 §1; r11.
