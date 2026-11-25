<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/labels.py, kept as a list -->

# Decisions behind `crsw_deposit/labels.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r5 Q2" means question 2 of
`DEPOSIT_TOOL_SPEC_R5.md`.

- **Decision:** every uploaded object carries four labels (dataset uuid, SHA-256 checksum, sensitivity, depositor), set at upload time, on members and on the dataset record alike.
  **Reason:** the labels are the minimal thread of metadata that travels with each object, whatever happens to the record.
  **Reference:** r5 Q2.
- **Decision:** the label names live in this module only, without a transport prefix.
  **Reason:** rclone wants `x-amz-meta-<name>` spelled out and boto3 adds the prefix itself. Each caller applies its own rule, and with one list of names the two routes can never disagree about them.
  **Reference:** r5 Q2.
