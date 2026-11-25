<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/cmd_common.py, kept as a list -->

# Decisions behind `promoter/cmd_common.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** provider slugs and place codes come from the vocabulary's providers and from `places.json` by its own chain.
  **Reason:** one source for each list.
  **Reference:** r11.
- **Decision:** a real run whose audit object cannot be written exits 2.
  **Reason:** a promotion without its trail is the one thing that must be noticed.
  **Reference:** none.
