<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/__main__.py, kept as a list -->

# Decisions behind `promoter/__main__.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** `recategorise` brings records in place up to the current vocabulary and applies a steward's change file.
  **Reason:** categories can change after deposit.
  **Reference:** r9.
- **Decision:** a deposit whose `derived_from` names another dataset in the store has the parent's uuid and version filled in before promotion.
  **Reason:** the web service cannot see destinations; the promoter can.
  **Reference:** r8 §4.
- **Decision:** a dataset leaves the store only through `remove`.
  **Reason:** the store changes only through the promoter, so every change is logged.
  **Reference:** r10.
- **Decision:** a promotion also builds passages when `PROMOTER_PASSAGES=1`.
  **Reason:** search inside documents.
  **Reference:** finding-and-reuse §7.
