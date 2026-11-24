<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/noise.py, kept as a list -->

# Decisions behind `crsw_deposit/noise.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r7 §2" means section 2 of
`DEPOSIT_TOOL_SPEC_R7.md`.

- **Decision:** OS and editor noise is excluded by default when a whole folder is walked, and never silently.
  **Reason:** nobody means to deposit `.DS_Store`, but dropping files without saying so would be a surprise. Callers report how many were dropped and offer a way to include them.
  **Reference:** r7 §2.
- **Decision:** `._*` files are noise.
  **Reason:** a Mac writing to exFAT or SMB leaves an AppleDouble sidecar beside every single file, invisible in Finder.
  **Reference:** r7 §2.
- **Decision:** the web route applies the same rules, through `is_noise_member`.
  **Reason:** browser folder pickers hand back the same files. The web route receives paths that are already relative and does not walk a filesystem, so it needs a check on a whole member path.
  **Reference:** r7 §2.
