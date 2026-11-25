<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/deposit_logic.py, kept as a list -->

# Decisions behind `crsw_deposit/deposit_logic.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r5 Q4" means question 4 of
`DEPOSIT_TOOL_SPEC_R5.md`, and "r8 §3" section 3 of `_R8.md`.

## The module

- **Decision:** what a deposit means is kept apart from how the bytes move.
  **Reason:** this was the orchestration inside the command line's `perform_deposits`. The command line (rclone) and the web service (boto3) both call it, so the record and labels are byte-identical whichever route a researcher used.
  **Reference:** `CLAUDE.md`, "every convention has one implementation".
- **Decision:** time and identity are passed in, never looked up.
  **Reason:** tests are then deterministic.

## Provenance file (`parse_provenance_file`)

- **Decision:** a `--provenance FILE` is a JSON list of activities, or an object with `provenance` and/or `derived_from`; only the shape is checked here.
  **Reason:** the full activity and reference checks need the manifest, so they run in `validate_record` once it exists. Strings in `derived_from` become references by the same rule as everywhere else.
  **Reference:** r8 §3.

## Keys (`plan_keys`)

- **Decision:** two members landing at the same key is an error.
  **Reason:** it would be a silent overwrite inside one batch.
- **Decision:** `display` lets the caller name each member in the collision message.
  **Reason:** the command line passes local paths; the web route can pass the browser's relative names.
- **Decision:** the per-part checks (red, reserved names, member-path grammar) are left to `keys.build_key`.
  **Reason:** one implementation.

## Dataset UUID (`dataset_uuid_for`)

- **Decision:** the UUID is minted once per prefix and kept on every later deposit.
  **Reason:** it identifies the dataset. It is kept for re-deposits to the same prefix and through staging; a deposit at a different state or sensitivity is a new prefix and a new UUID.
  **Reference:** r5 Q1.

## Inherited fields (`INHERITED_FIELDS`, `KEPT_UNLESS_SUPPLIED`, `defaults_from_record`)

- **Decision:** a deposit to an existing dataset starts from the record's descriptive fields.
  **Reason:** the command line offers them as prompt defaults and the web form prefills from them, so the two routes inherit the same things.
  **Reference:** r5 §4.
- **Decision:** `derived_from` is not among the inherited fields.
  **Reason:** `assemble_record` keeps the existing list unless this deposit supplies one, so a form must not echo it back.
  **Reference:** r8 §3.
- **Decision:** `provider`, `spatial`, `spatial_note`, `notes`, `language` and `ethics_ref` are kept from the existing record unless the deposit supplies its own.
  **Reason:** the `derived_from` rule applied to the rest of the optional fields: a re-deposit never silently drops them.
  **Reference:** r11.

## Versions (`version_not_lower`)

- **Decision:** a lower version than the record's is flagged; anything that is not a version compares as fine.
  **Reason:** the case in mind is a re-deposit with the form's default 1-0 over a dataset at 3-0. Malformed versions are for the validators to explain.

## Assembling the record (`assemble_record`)

- **Decision:** the manifest is the union of existing and batch; nothing is removed.
  **Reason:** deposits are additive; removing a member is deliberate curation done elsewhere.
  **Reference:** r5 Q4.
- **Decision:** the coverage envelope widens over every member's own range and the old envelope.
  **Reason:** older members whose per-file coverage was never recorded stay contained.
  **Reference:** r5 Q5.
- **Decision:** `created` is the existing record's, else the caller's, else now.
  **Reason:** the promoter knows when the dataset first came into being (the staged record's `created`) and passes it.
- **Decision:** depositors accumulate in first-seen order with no repeats; provenance activities accumulate as the existing list followed by this deposit's.
  **Reference:** r5 Q4; r8 §3.
- **Decision:** category history accumulates the same way.
  **Reason:** the promoter adds entries when it maps a staged record's stale terms.
  **Reference:** r9.
- **Decision:** the result is not validated here.
  **Reason:** callers have the vocabulary; they run `record.validate_record` and treat errors as fatal before writing.

## Record bytes (`record_bytes`)

- **Decision:** a record is stored as UTF-8 with LF line endings and a trailing newline, from this one serialiser.
  **Reason:** the record's own checksum label is computed from these bytes, so they must not vary by platform or by route.
  **Reference:** r7 §4.

## Completion check (`completion_problems`)

- **Decision:** a deposit is complete when every manifest entry exists in the store at the recorded size.
  **Reference:** r5 Q4.
- **Decision:** sizes are compared, never a SHA-256 against an ETag.
  **Reason:** a multipart upload's ETag is a hash of hashes, not the file's checksum.
  **Reference:** r2 §0.
