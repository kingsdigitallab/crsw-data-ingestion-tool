# CRSW Deposit Tool — revision spec (r13): programs and macro files are not deposited

Status: **proposal, not agreed.** Built on 19 November 2026. Read
alongside r7 §2 (OS noise) and §3 (reserved names), which are the two
earlier rules about which files a deposit may hold, and r10 (the store
changes only through the promoter). Nothing here changes the record
schema, the key layout, the object labels or the
members-first-record-last rule.

## 1. Why

Nothing in the tool runs a deposited file. No code imports, executes or
unpacks one; the only content read is plain text, PDF and `.docx`, for
passages. Every download is sent as an attachment of unspecified type
(`application/octet-stream`, `Content-Disposition: attachment`,
`X-Content-Type-Options: nosniff`, a `default-src 'self'` policy), so a
browser saves it and never renders it.

What is left is a colleague downloading a deposited program and running
it. The store is for data, documents and analysis code, so programs and
their relatives are refused at deposit.

## 2. What is refused

**By name** (the file name's last extension, case ignored, trailing dots
and spaces dropped first because Windows ignores them):

| Kind, as the depositor reads it | Extensions |
|---|---|
| a Windows program | exe dll com scr sys drv ocx cpl efi pif xll |
| an installer | msi msp mst msix msixbundle appx appxbundle appinstaller application dmg pkg mpkg deb rpm apk ipa appimage |
| a macOS or Linux program | app command so dylib ko run |
| a system script | bat cmd ps1 psm1 psd1 ps1xml vbs vbe jse wsf wsh wsc sct hta msc reg inf sh bash zsh ksh csh tcsh fish |
| a Java program | jar class jnlp |
| a shortcut | lnk url scf desktop |
| an Office file with macros | docm dotm xlsm xltm xlam xla pptm potm ppsm ppam sldm accde mde ade |

Only the file name counts; a folder called `tools.exe/` refuses nothing.

**By opening bytes**, whatever the name, from the first 512 bytes:

- a Windows program: `MZ` with a NUL byte among the next 62 (a real
  header always has them; a CSV whose first cell is `MZ`, Mozambique's
  code, has none);
- a Linux program: `7F 45 4C 46`;
- a macOS program: the Mach-O numbers `FEEDFACE`, `FEEDFACF`, their
  byte-swapped forms, and the universal-binary `CAFEBABE`/`CAFEBABF`
  and their swapped forms (`CAFEBABE` is also a Java class file; both
  are refused, and the message says "a macOS or Java program");
- a shell script: a `#!` first line whose interpreter, named directly
  or through `env`, is one of sh, bash, dash, zsh, ksh, ksh93, mksh,
  pdksh, csh, tcsh, fish, ash, busybox, pwsh, powershell. This is the
  same rule as refusing `.sh` by name.

## 3. What is deliberately allowed

- **Analysis code:** `.py .R .do .ipynb .sql .js .pl .rb .m .jl .sas .sps`
  and the like, including with a `#!/usr/bin/env python` or `Rscript`
  first line. It is text until somebody chooses to run it, and a
  dataset may carry the code that made it.
- **`.bin .img .out .dat`:** real data formats (rasters, model output).
  A true program under one of these names is caught by its opening
  bytes.
- **`.doc .xls .ppt .xlsb .mdb .accdb`:** ordinary research material,
  though they can carry macros (§6).
- **`.html .svg`:** never rendered from the store.
- **Archives** (`zip 7z rar tar gz tgz bz2 tbz tbz2 xz txz zst tzst lz
  lz4 lzma z cab iso`): accepted and never opened. The depositor is
  told nobody checks what is inside; the dataset page says the same
  beside each archive and once above the list.

A consequence to know: shell scripts, including batch-scheduler job
scripts, are refused even under another name. The route for a folder of
code that includes them is to deposit it as an archive.

## 4. Where the rule lives and where it is called

One implementation, `crsw_deposit/filetypes.py` (stdlib only):
`refused_name`, `refused_content`, `is_archive`, `sniff_file`,
`SNIFF_BYTES`. It is called where a file **enters**, and nowhere else:

| Route | Where | What happens |
|---|---|---|
| CLI | `deposit.py`, after the reserved-name check and before preflight | The run stops, listing every such file and why; nothing is uploaded. Works offline and under `--dry-run`. A name typed at the rename prompt is checked too. Archives get one `note:` line. |
| Web upload | `PUT /deposits/{id}/files/{member}` | 422 for the name before any byte is read; 422 for the opening bytes before anything is written to staging (`upload.stream_to_s3`'s `head_check`). An earlier copy of the same member is left as it was. |
| Web form | `deposit.js`, from the lists in `client_rules()` | The row is marked "not accepted: …" and not sent; the rest of the deposit goes ahead. The page reads each file's first 512 bytes locally to catch a renamed program before it is sent. The server remains the authority. |
| Bulk route | `crsw_web.bulk.stage_dataset` | `BulkError` before any write, dry run included. |
| Promoter | `promoter.checks.check`, over the staged deposit's own members | Problems `member type: …` and `member content: …`; the deposit stays in staging. The opening bytes come from the read that re-hashes the member (a 512-byte ranged read when checksums are off). Archives are a warning. |

`promoter remove --staging ID` may clear a finished deposit refused for
these two problems: they are the deposit's own content
(`remove.CONTENT_REFUSALS`).

**Never in `record.validate_record`, `dataset.schema.json` or
`keys.build_key`.** Those also run over records already in the store:
when a re-deposit is merged, on `recategorise`, on the vocabulary
replay, and when a file is deleted from an open deposit. A program
deposited before this rule would otherwise block every one of them. It
stays valid in its record, listed and downloadable; removing it is a
separate decision through `promoter remove`. Tests pin this
(`test_a_program_in_the_manifest_is_not_a_record_error`,
`test_a_program_already_in_the_dataset_does_not_block_a_new_deposit`).

## 5. How far each route can be relied on

- **Web and bulk:** enforced twice, by the service and by the promoter,
  with different credentials.
- **CLI:** the check is the tool's own. The CLI writes straight to the
  store with the depositor's key and does not pass the promoter, so it
  stops mistakes, not a person who edits the tool or uses rclone
  directly. That is the same standing as every other CLI rule.

## 6. Known limits

Stated, not solved:

- an Office file with macros renamed `.docx`, `.xlsx` or `.pptx`
  (telling needs the file's inner listing read);
- macros in the older `.doc`, `.xls`, `.ppt` and in `.xlsb`;
- anything inside an archive or disk image;
- a header crafted to dodge the byte tests;
- analysis code is code: a `.py` or `.js` file does whatever it says if
  somebody runs it.

## 7. Personal data

None is newly stored, logged or sent. The 512 bytes are looked at in
memory and dropped; the browser's own look never leaves the page. The
promoter's trail already names the files a refused deposit holds.
`/privacy` is unchanged.
