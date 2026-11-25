<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/remove.py, kept as a list -->

# Decisions behind `promoter/remove.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** datasets leave the store only through the promoter.
  **Reason:** this extends the rule for categories: every change to the store is a promoter action, and logged.
  **Reference:** r10, extending r9.
- **Decision:** `inspect` refuses a record whose identifier disagrees with its location.
  **Reason:** the typo guard.
  **Reference:** r10.
- **Decision:** a staging deposit is looked for under every user.
  **Reason:** the operator has the id from a log, not the user.
  **Reference:** r10.
- **Decision:** a finished staging deposit may go only when the promoter's own checks refuse it for its own content.
  **Reason:** removing it is then the fix. Any other refusal (the vocabulary fetched, authorisation, size caps, the destination record) may be the VM's configuration, and the deposit would promote once that is right.
  **Reference:** r12.
- **Decision:** a member that is a program counts as a content refusal.
  **Reason:** the deposit can never be promoted as it is.
  **Reference:** r13.
