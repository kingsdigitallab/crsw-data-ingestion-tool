<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/promote.py, kept as a list -->

# Decisions behind `promoter/promote.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** references the checks resolved are written to the staged record first, then promoted like everything else.
  **Reason:** what was moved is what was checked.
  **Reference:** r8 §4 (decided).
- **Decision:** when the checks mapped stale subject terms, the record's history says so.
  **Reason:** the record carries its own history of category changes.
  **Reference:** r9.
