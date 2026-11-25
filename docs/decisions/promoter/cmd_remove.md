<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/cmd_remove.py, kept as a list -->

# Decisions behind `promoter/cmd_remove.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** a dataset leaves the store only through this command; the plan is shown unless `--confirm` repeats every id.
  **Reason:** nobody deletes under a strand prefix by hand, and a typo must not remove the wrong dataset.
  **Reference:** r10.
- **Decision:** the person removing must be someone `PROMOTER_AUTHORISED` would let deposit to that strand.
  **Reason:** the same policy as a deposit, in reverse.
  **Reference:** r10.
- **Decision:** a finished staging deposit may be removed only when the promoter's own checks refuse it.
  **Reason:** see `remove.md`, content refusals.
  **Reference:** r12.
