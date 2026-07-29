# crsw-data-ingestion-tool

Command-line tool for depositing research files into the CRSW shared object
storage (Ceph, S3-compatible, hosted by KCL eResearch). It builds object
keys from the Centre's path convention, uploads the files via rclone, and
maintains one `dataset.meta.json` record per dataset describing the whole
prefix - shared metadata plus a manifest of every member file.

## Requirements

- Python 3.8 or newer (standard library only — nothing to install)
- [rclone](https://rclone.org/downloads/) — installed, or the binary dropped
  in this folder
- An rclone remote for the storage service. Add this to your rclone config
  (`rclone config file` shows where it lives; keys come from eResearch):

```ini
[ceph]
type = s3
provider = Ceph
access_key_id = YOUR_ACCESS_KEY
secret_access_key = YOUR_SECRET_KEY
endpoint = https://OBJECT-STORE-ENDPOINT-FROM-ERESEARCH
```

- The KCL VPN — the storage endpoint is not reachable without it.

## Usage

```
python deposit.py FILE_OR_GLOB [FILE_OR_GLOB ...] [options]

  --strand rs2            skip strand prompt (rs1/rs2/rs3/rs4)
  --project csac          skip project prompt (a name new to the strand
                          is confirmed once before creating it)
  --domain quant          skip domain prompt (codes from the vocabulary)
  --state 2_final         skip state prompt (0_raw/1_interim/2_final)
  --sensitivity green     skip sensitivity prompt (green/amber)
  --dry-run               preview keys and sidecars, upload nothing
  --remote NAME           rclone remote (default: saved config, else ceph)
  --bucket NAME           target bucket (default: saved config, else crsw)
  --reconfigure           re-run the remote/bucket setup prompts
  --verbose               show raw rclone output on errors
```

Anything not supplied as a flag is prompted for. Prompts with a fixed set of
answers show a numbered menu — type the number or the value, either works.
After the strand is chosen, existing projects in it are listed to pick from
(`n` starts a new one). New project names are normalised (lowercase, hyphens)
and need confirmation, with a warning when the name is close to an existing
project. A fully-flagged `--dry-run` is the way to check a deposit before
committing to it, and the way to reproduce a problem when asking for support.

Files land at `{strand}/{project}/{sensitivity}/{state}/{filename}`. That
prefix is a *dataset*: its `dataset.meta.json` holds the description and a
`files` manifest (path, SHA-256, size), and every uploaded object carries
dataset-uuid, checksum, sensitivity and depositor labels. Member files
upload first and the record is written last, so its presence marks a
complete deposit - an interrupted batch simply re-runs, skipping files
that already match the manifest. Depositing again to the same prefix loads
the existing record as defaults instead of re-asking everything, and adds
to the manifest (never removes; curation is a deliberate act done
elsewhere). `dataset.meta.json` is therefore a reserved filename.

Filenames are preserved exactly as deposited; if a name contains awkward
characters the tool offers a correction but never applies one silently.
After upload, every manifest entry is verified to exist at the expected
size before the deposit is reported done.

Red-classified data is refused — it belongs in the TRE, not shared storage.

## Configuration

On first run the tool asks which rclone remote and bucket to use (picking
the remote from a menu of those you already have) and saves the answer to
`%LOCALAPPDATA%\crsw-deposit\config.json` (Windows) or
`~/.config/crsw-deposit/config.json` (macOS/Linux). Precedence is
command-line flag → saved config → built-in default. Run with
`--reconfigure` to change the saved values. Every run starts with a
`Target:` line showing the resolved `remote:bucket` and where each value
came from, so there is never any doubt about which storage a deposit used.

## Subjects and domains vocabulary

Subject terms and data domains are validated against the shared vocabulary,
fetched live so newly approved terms are available immediately. If the fetch
fails (offline, VPN restrictions) the tool falls back to a cached copy, then
to the bundled `vocab.json`, and says which one it used. The subject listing
is shown, numbered, before you're asked — answer with numbers, terms, or a
mix. Unknown terms are refused — propose additions via the vocabulary
repository or your domain steward. Choosing a domain fills in its steward
automatically.

## Output

Colour is used sparingly (warnings yellow, errors red, success green) and
never as the only signal. Set `NO_COLOR=1` to disable it, `FORCE_COLOR=1`
to force it on; piped or redirected output is always plain.

## Deposit log

Every successful deposit appends a line (timestamp, key, checksum, depositor)
to `deposits.log` in `%LOCALAPPDATA%\crsw-deposit\` (Windows) or
`~/.cache/crsw-deposit/` (macOS/Linux).

## Development

Run the tests with:

```
python -m unittest discover -s tests -v
```

`dataset.schema.json` is the contract for the record format, used by CI
and the future ingestion gateway; the shipped tool validates by hand
(standard library only). Tests that need the `jsonschema` package skip
cleanly when it is not installed. `export_dcat.py` converts a record to
a DCAT (JSON-LD) dataset description and doubles as the proof that the
Dublin Core mapping is complete.

The build spec is `DEPOSIT_TOOL_SPEC.md`, revised by `DEPOSIT_TOOL_SPEC_r2.md`
(r2 supersedes where it speaks). Module boundaries and constraints are
documented there and in `CLAUDE.md`.
