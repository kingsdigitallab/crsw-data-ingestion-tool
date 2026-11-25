<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/filetypes.py, kept as a list -->

# Decisions behind `crsw_deposit/filetypes.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r13" is `DEPOSIT_TOOL_SPEC_R13.md`.

- **Decision:** programs, installers, system scripts, shortcuts and Office files with macros are refused at deposit.
  **Reason:** the store holds data, documents and analysis code. Nothing in the service ever runs a deposited file, but a colleague who downloads one might.
  **Reference:** r13.
- **Decision:** a file is refused twice over, by its last extension and by its opening bytes.
  **Reason:** a program renamed `results.csv` is still a program.
  **Reference:** r13.
- **Decision:** analysis code (`.py`, `.R`, `.do`, `.ipynb`, `.sql`, `.js` ...) is deliberately allowed.
  **Reason:** it is text until somebody chooses to run it, and a dataset may carry the code that made it.
  **Reference:** r13.
- **Decision:** archives are allowed and never opened.
  **Reason:** opening them is out of scope; callers say "contents not checked" to the depositor and to whoever downloads.
  **Reference:** r13.
- **Decision:** the checks are called only where a file enters (the CLI, the web upload, the bulk route, the promoter's check of a staged deposit), and never from `record.validate_record`, the schema or `keys.build_key`.
  **Reason:** those three also run over records already in the store (merge, recategorise, replay). A program deposited before the rule existed must not block them.
  **Reference:** r13.
- **Decision:** trailing dots and spaces are dropped before the extension is read.
  **Reason:** Windows ignores them, so `x.exe.` runs as `x.exe`.
  **Reference:** r13.
- **Known limits, stated and not solved:** a macro file renamed `.docx` or `.xlsx`; macros in legacy `.doc` and `.xls`; whatever is inside an archive or disk image; a header crafted to dodge the byte tests.
  **Reference:** r13.
