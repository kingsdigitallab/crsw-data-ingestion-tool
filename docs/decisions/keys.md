<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/keys.py, kept as a list -->

# Decisions behind `crsw_deposit/keys.py`

The docstrings in `keys.py` say what each function does and returns. The
reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r6 §2" means section 2 of
`DEPOSIT_TOOL_SPEC_R6.md`, and "spec" means the original
`DEPOSIT_TOOL_SPEC.md`.

## The module

- **Decision:** key construction lives apart from `record.py`.
  **Reason:** the path convention may change independently of the metadata schema.
  **Reference:** handbook v0.4 §4.
- **Decision:** object keys always use `/`, on every platform.
  **Reason:** a key is a storage name, not a local path; a backslash from Windows would make a different object.
  **Reference:** handbook v0.4 §4.

## Strands

- **Decision:** `STRANDS` includes `centre`, `DEPOSIT_STRANDS` does not.
  **Reason:** `centre` is curated by the Centre and written only by the bulk route (`crsw_web.bulk`); the form and the command line offer `rs1` to `rs4`, while search and the promoter read every strand.
  **Reference:** r12.

## Red data (`RedDataError`, `_validate_parts`)

- **Decision:** sensitivity `red` is refused at key construction.
  **Reason:** red data never enters shared storage; it belongs in the TRE.
  **Reference:** handbook §3 and §7.3.
- **Decision:** the refusal is repeated here although `deposit.py` refuses red earlier.
  **Reason:** it is a backstop, so a route that never goes through `deposit.py` still cannot build a key for red data.
  **Reference:** handbook §3 and §7.3.

## Project and dataset names (`validate_project`, `normalise_project`, `similar_projects`)

- **Decision:** one slug rule (lowercase ASCII letters and digits, hyphen-separated) covers both project and dataset names.
  **Reason:** the dataset is a path element in its own right, like the project.
  **Reference:** r6 §1.
- **Decision:** the near-match test is deliberately not clever: equal ignoring case and hyphens, one name inside the other, or a trailing `s`.
  **Reason:** it catches the near-duplicates that actually happen. Exact matches are left out because the caller handles "already exists" separately.
  **Reference:** r4 §2.

## Filenames (`filename_problems`, `suggest_filename`)

- **Decision:** only spaces and `\ / : * ? " < > |` are flagged; anything else is preserved.
  **Reason:** filenames are kept as deposited.
  **Reference:** spec §4.
- **Decision:** a suggested correction is never applied automatically.
  **Reason:** the user must accept or edit it; a silent rename would break the link between the deposit and the depositor's own files.
  **Reference:** spec §4.

## The reserved record name (`RESERVED_RECORD_RE`, `is_reserved_name`, `is_reserved_member`, `record_filename`)

- **Decision:** the whole `dataset.*.json` shape is reserved, not one fixed name.
  **Reason:** the record is `dataset.<slug>.json`, named for its dataset.
  **Reference:** r6 §2.
- **Decision:** the reservation applies to every segment of a member path, not only the last.
  **Reason:** the dataset is a real path element, so a nested `dataset.sub.json` looks exactly like the record of a dataset called `sub` to anything walking the tree.
  **Reference:** r6 §2.
- **Decision:** the record's slug always equals the last element of the prefix.
  **Reason:** any consumer can build the record key from the prefix alone, without listing.
  **Reference:** r6 §2.

## Member paths (`normalise_member_path`, `member_path_error`, `member_path_problems`, `suggest_member_path`)

- **Decision:** a member path may contain `/`, preserving a file's sub-path relative to the deposit argument's root.
  **Reason:** folders can be deposit arguments. The same string is the key's last element and the manifest `path`, which is why it is validated in one place.
  **Reference:** r7 §1.
- **Decision:** each segment is normalised to composed (NFC) Unicode in the key and the manifest; the local file on disk is never touched.
  **Reason:** macOS returns decomposed (NFD) Unicode where Windows and Linux return composed, so the same accented filename deposited from a Mac and a PC would otherwise give two manifest entries and two objects for identical content.
  **Reference:** r7 §4.
- **Decision:** structural problems (`member_path_error`) are refused outright and never offered as "keep as-is"; the problem-character checks (`member_path_problems`) are advisory and never block.
  **Reason:** the first is about the grammar of keys and manifests; the second is about how friendly a name is to the filesystems of people who later download it.
  **Reference:** r7 §1; spec §4.
- **Decision:** the advisory check must run per segment.
  **Reason:** `PROBLEM_CHARS` includes `/`, so the plain filename check on a whole member path would flag every separator.
  **Reference:** r7 §1.
- **Decision:** a suggested member path is never applied automatically.
  **Reason:** as for filenames.
  **Reference:** spec §4.

## Prefixes and keys (`dataset_prefix`, `record_key`, `build_key`)

- **Decision:** the layout is `{strand}/{project}/{sensitivity}/{state}/{dataset}/{member}`; the dataset is a real path element and a project may hold several.
  **Reason:** two datasets in one project each keep their own files with no collision. The prefix is also the record's `identifier` field.
  **Reference:** r6 §1.
- **Decision:** the member path is validated inside `build_key`, not only by callers.
  **Reason:** it is the one place where "never let `os.sep` leak into a key" can be guaranteed for every caller, including a route that never goes near `deposit.py`. The checks are backstops: `deposit.py` refuses the same things earlier.
  **Reference:** r7 §1.
