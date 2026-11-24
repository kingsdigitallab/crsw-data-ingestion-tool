<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/config.py, kept as a list -->

# Decisions behind `promoter/config.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** the LLM platform is off unless both URL and key are set; a blank model name switches that function off.
  **Reason:** every function has a switch, and off is not a failure.
  **Reference:** finding-and-reuse §5.
- **Decision:** passages are off unless `PROMOTER_PASSAGES=1`, and capped by file size and by passages per dataset.
  **Reason:** one mistaken deposit must not fill the VM.
  **Reference:** finding-and-reuse §7.
