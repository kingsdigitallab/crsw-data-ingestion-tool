<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings and comments in promoter/embed.py, kept as a list -->

# Decisions behind `promoter/embed.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r8 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R8.md`. "finding-and-reuse" is
`docs/web-deposit/finding-and-reuse.md`.

- **Decision:** embeddings are a file of their own beside the index.
  **Reason:** the human-readable index stays lean, and this file can be dropped or rebuilt on its own.
  **Reference:** finding-and-reuse §5.
- **Decision:** only the record's title, abstract, subject terms and source note are embedded; nothing from the manifest or from any file.
  **Reason:** only record metadata goes to the platform.
  **Reference:** finding-and-reuse §5.
- **Decision:** a subject term is sent as its label and scope note, never as the bare slug.
  **Reason:** "Forced labour: work exacted under the menace of a penalty" carries meaning a slug does not.
  **Reference:** vocabulary-hierarchy-and-scope-notes handoff.
- **Decision:** a row is kept only if its identifier, modified stamp, model and `text_sha` all match the previous file.
  **Reason:** a routine run embeds only what changed, and an edited scope note or label re-embeds exactly the rows carrying that term.
  **Reference:** same.
- **Decision:** `model` is the fingerprint of what the alias resolves to, not the alias.
  **Reason:** a backend swapped behind an unchanged alias must re-embed every row, since vectors from different models cannot be compared.
  **Reference:** embedding-model-fingerprint handoff.
- **Decision:** records of sensitivities that are not allowed are written with no vector.
  **Reason:** green until the Centre allows amber; the row still says the dataset exists.
  **Reference:** finding-and-reuse §5.
- **Decision:** vectors are stored as 16-bit floats.
  **Reason:** the ranking cannot tell half precision from full, at a quarter of the storage with the shortened vector.
  **Reference:** `crsw_web/llm.py`.
- **Decision:** a row without a vector is not counted as stale or missing.
  **Reason:** nothing was made from its text, and its sensitivity is not sent. A missing row is a dataset promoted while the platform could not be used.
  **Reference:** none.
