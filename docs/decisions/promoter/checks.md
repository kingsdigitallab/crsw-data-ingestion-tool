<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/checks.py, kept as a list -->

# Decisions behind `promoter/checks.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** with the whole vocabulary to hand, a staged record whose subject terms have since been renamed, merged or retired is mapped and promoted with the mapping in its history; one whose term was split is refused.
  **Reason:** a split needs a person to choose.
  **Reference:** r9.
- **Decision:** a `derived_from` parent that is not in the store is a warning; the reference is left as typed and the deposit goes on.
  **Reason:** resolve, don't invent. A researcher may deposit a final dataset before its interim sibling.
  **Reference:** r8 §4.
- **Decision:** the record's provider and spatial are checked against the lists in hand.
  **Reason:** as for a deposit.
  **Reference:** r11.
- **Decision:** a member that starts like a program is refused even under another name; only what this deposit brings is judged, never what is already in the dataset.
  **Reason:** a program from before the rule must not block a new deposit.
  **Reference:** r13.
- **Decision:** `_head_of_object` exists beside `_sha256_and_head`.
  **Reason:** when checksums are not being verified nothing else reads the object, but its opening bytes are still needed for the file-type check.
  **Reference:** r13.
