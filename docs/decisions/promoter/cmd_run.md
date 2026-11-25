<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/cmd_run.py, kept as a list -->

# Decisions behind `promoter/cmd_run.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** only a vocabulary fetched from its repository is replayed over the records.
  **Reason:** after a failed fetch the cache or bundled copy may be older than the marker, and would refuse records that already carry newer terms.
  **Reference:** decision recorded in CLAUDE.md, separate promoter.
- **Decision:** nothing in the replay may stop the run.
  **Reason:** promotion is done by then and the index still has to be written. A record the replay refuses holds the marker back, so the next run tries again.
  **Reference:** same.
- **Decision:** a run narrowed to one user or one deposit does not replay.
  **Reason:** it is about those deposits, not the whole store.
  **Reference:** same.
- **Decision:** references the store resolved are logged as `resolved_reference`.
  **Reason:** what the store knows about the parent, filled in.
  **Reference:** r8 §4.
