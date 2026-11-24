<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/resolve.py, kept as a list -->

# Decisions behind `promoter/resolve.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** the promoter fills in a parent dataset's uuid and version.
  **Reason:** a web deposit cannot see destinations, so the reference arrives with only the identifier the researcher typed; uuid and version are what a catalogue keys on.
  **Reference:** r8 §4.
- **Decision:** a parent that is not there is a warning, never a block.
  **Reason:** a researcher may deposit a final dataset before its interim sibling.
  **Reference:** r8 §4.
