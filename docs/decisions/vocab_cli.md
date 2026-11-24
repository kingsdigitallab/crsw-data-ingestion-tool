<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/vocab_cli.py, kept as a list -->

# Decisions behind `crsw_deposit/vocab_cli.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r9 §1.6" means section 1.6 of
`DEPOSIT_TOOL_SPEC_R9.md`.

- **Decision:** `crsw-vocab` exists so that a steward can edit the term authority file without typing JSON.
  **Reference:** r9 §1.6.
- **Decision:** the file is validated after each change and written only if it passes; the input file is never touched on a refusal.
  **Reason:** a half-applied change to the vocabulary would reach every tool that fetches it.
  **Reference:** r9 §1.6.
- **Decision:** `describe` writes no entry in the change list and leaves the version alone.
  **Reason:** a scope note and other spellings are description, not a change to which records a term applies to. The git history of the vocabulary repository is the record of such edits.
  **Reference:** r9 §1.6.
- **Decision:** the command makes no git calls.
  **Reason:** the person commits the result, or the vocabulary repository's Action does.
- **Decision:** this module talks to the user.
  **Reason:** it is a command. Apart from `deposit.py`, which is outside the package, it is the only place that does; the rest of `crsw_deposit` has no user I/O.
