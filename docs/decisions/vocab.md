<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/vocab.py, kept as a list -->

# Decisions behind `crsw_deposit/vocab.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r2 §2" means section 2 of
`DEPOSIT_TOOL_SPEC_R2.md`, and "spec" means the original
`DEPOSIT_TOOL_SPEC.md`.

- **Decision:** the vocabulary is loaded by a chain: HTTPS fetch with a short timeout, then the local cache, then the bundled `vocab.json`.
  **Reason:** newly approved terms are available at once, and a slow or blocked network must never block a deposit. Callers decide how to tell the user which source was used.
  **Reference:** spec §6.
- **Decision:** a fetched or cached file is used only if it is the authority shape and passes every authority rule, or is the older flat shape with facet lists.
  **Reason:** a bad file from the network must not be trusted; it is ignored and the chain falls through.
  **Reference:** r9.
- **Decision:** the cache write is best-effort and atomic; failing to cache is never an error. `places.json` is cached beside the vocabulary the same way.
  **Reason:** a read-only or full disk should cost a deposit nothing.
  **Reference:** spec §6; r11.
- **Decision:** the flat facet lists are derived from the term entries when the file is an authority file.
  **Reason:** older readers keep working, with one source of truth.
  **Reference:** r9.
- **Decision:** domains come from the vocabulary, with the built-in codes as a fallback.
  **Reason:** domains are fetched, not hardcoded, but an old cached vocabulary with no domains must never break a deposit.
  **Reference:** r2 §2.
- **Decision:** provenance activity kinds are managed in the vocabulary, with the built-in list as a fallback.
  **Reason:** a strand (RS3 was the example) can add its own steps without a release of the tool.
  **Reference:** r8 §2.
- **Decision:** providers have no built-in fallback.
  **Reason:** an empty list means the vocabulary in hand has no providers section; validation then warns about an unknown slug and does not refuse it.
  **Reference:** r11 §3.
- **History:** the comment above `VOCAB_URL` used to read "Single line to update once the vocabulary repo is created". The repository exists (`kingsdigitallab/crsw-vocabulary`), so the comment now says what the constant is.
