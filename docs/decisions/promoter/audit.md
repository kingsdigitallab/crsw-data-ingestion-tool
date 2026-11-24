<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/audit.py, kept as a list -->

# Decisions behind `promoter/audit.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** each run's log lines are also written, once, as one object in the bucket.
  **Reason:** journald on the internal VM is size-capped and dies with the VM. The key the promoter already holds is enough to write it.
  **Reference:** decision of 18 September 2026 (CLAUDE.md, separate promoter).
- **Decision:** the trail is not protected by anything in this module.
  **Reason:** the bucket is versioned, so the trail cannot be edited in place.
  **Reference:** none.
