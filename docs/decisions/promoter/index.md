<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/index.py, kept as a list -->

# Decisions behind `promoter/index.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** the index is written as JSON lines and as Parquet.
  **Reason:** Parquet is readable straight from S3 by DuckDB, cdisaw-parquet or a laptop; the JSON lines need nothing. pyarrow is in the `web` extra the image installs.
  **Reference:** none.
- **Decision:** the bucket is the truth and the records are the source; the index is a cache.
  **Reason:** anything can rebuild it with `python -m promoter index`.
  **Reference:** none.
- **Decision:** `read_rows` reads the index in place with one GET.
  **Reason:** the quiet run's embedding check must not read every record every five minutes.
  **Reference:** none.
- **Decision:** `domain` in a row stays the primary code and `domains` is the whole list.
  **Reason:** every reader of the index expects `domain` as a string.
  **Reference:** r11.
- **Decision:** `spatial` is written as recorded.
  **Reason:** the find page expands the places hierarchy at query time, as it does the subjects.
  **Reference:** r11.
- **Decision:** a 0.5 record's string `derived_from` is a reference like any other by the time it is read.
  **Reason:** records are upgraded to the current schema on read.
  **Reference:** r8 §5.
