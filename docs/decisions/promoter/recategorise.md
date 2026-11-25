<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/recategorise.py, kept as a list -->

# Decisions behind `promoter/recategorise.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** records in place are brought up to the current categories by rewriting the record only, never a member, a label or a key.
  **Reason:** categories change after deposit; the files do not.
  **Reference:** r9.
- **Decision:** a change file can also set descriptive fields in place.
  **Reason:** licence, notes, creator and the like sometimes need correcting without a new deposit.
  **Reference:** r11.
- **Decision:** a marker that cannot be read is treated as never having applied.
  **Reason:** a replay is safe to repeat.
  **Reference:** none.
- **Decision:** bytes that are not UTF-8 are one record's problem, not the whole walk's.
  **Reason:** a record may have been saved through the mount by something else.
  **Reference:** none.
