<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/cmd_derived.py, kept as a list -->

# Decisions behind `promoter/cmd_derived.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** the index is never rewritten on a dry run or when nothing changed.
  **Reason:** the bucket is versioned; a write every five minutes would pile up versions.
  **Reference:** none.
- **Decision:** a failure to write the index is logged and does not change the exit code.
  **Reason:** the index is a cache that `promoter index` rebuilds.
  **Reference:** none.
- **Decision:** a run that changed nothing still asks what the embed alias resolves to, and replaces every vector when that has moved.
  **Reason:** every vector in place is then from another model, and vectors from different models cannot be compared. It costs one GET for the fingerprint, one for the previous file and one for the index.
  **Reference:** `docs/web-deposit/embedding-model-fingerprint-handoff.md`.
- **Decision:** rows whose text the vocabulary has since changed (a scope note or label edited) are embedded again.
  **Reason:** the vector should match the text a search would compare it with.
  **Reference:** `docs/web-deposit/vocabulary-hierarchy-and-scope-notes-handoff.md`.
- **Decision:** the model a step's vectors are keyed to is written to the trail.
  **Reason:** the audit then shows what made the vectors and when it changed.
  **Reference:** embedding-model-fingerprint handoff.
- **Decision:** when the platform does not name a backend for the alias, nothing is written.
  **Reason:** vectors keyed to the bare alias would be replaced wholesale by the next run that resolves it.
  **Reference:** embedding-model-fingerprint handoff.
- **Decision:** a PDF with no text layer keeps its marker when no OCR model is named.
  **Reason:** OCR is a switch; off is not a failure.
  **Reference:** `docs/web-deposit/ocr-handoff.md`.
