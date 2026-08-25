# crsw-data-ingestion-tool

Command-line tool for depositing research files into the CRSW shared object
storage (Ceph, S3-compatible, hosted by KCL eResearch). It builds object
keys from the Centre's path convention, uploads the files via rclone, and
maintains one `dataset.<name>.json` record per dataset describing the
whole prefix - shared metadata plus a manifest of every member file.

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

  --include-noise         deposit OS/editor noise files (.DS_Store,
                          Thumbs.db, ._* etc.) instead of excluding them
  --strand rs2            skip strand prompt (rs1/rs2/rs3/rs4)
  --project csac          skip project prompt (a name new to the strand
                          is confirmed once before creating it)
  --dataset sentinel2-imagery  skip dataset prompt (a project may hold
                          several; a new name is confirmed once, same
                          as project)
  --domain quant          skip domain prompt (codes from the vocabulary)
  --state 2_final         skip state prompt (0_raw/1_interim/2_final)
  --sensitivity green     skip sensitivity prompt (green/amber)
  --dry-run               preview keys and the dataset record, upload nothing
  --remote NAME           rclone remote (default: saved config, else ceph)
  --bucket NAME           target bucket (default: saved config, else crsw)
  --reconfigure           re-run the remote/bucket setup prompts
  --verbose               show raw rclone output on errors
```

Anything not supplied as a flag is prompted for. Prompts with a fixed set of
answers show a numbered menu — type the number or the value, either works.
After the strand and project are chosen, existing datasets in that project
are listed to pick from (`n` starts a new one) — the same picker used for
the project itself, one level down. New project or dataset names are
normalised (lowercase, hyphens) and need confirmation, with a warning when
the name is close to an existing one; confirming a genuinely new project or
dataset also asks whether access will need restricting beyond the strand —
say yes and the tool stops, because that restriction has to exist before
the first deposit, not be applied afterwards. A fully-flagged `--dry-run`
is the way to check a deposit before committing to it, and the way to
reproduce a problem when asking for support.

Files land at `{strand}/{project}/{sensitivity}/{state}/{dataset}/{filename}`
— a project can hold several distinct datasets, each in its own prefix, so
two datasets can each have their own `readme.md` with no collision. That
prefix's record, `dataset.<name>.json` (named for the dataset, so several
downloaded together never collide), holds the description and a `files`
manifest (path, SHA-256, size, format), and every uploaded object carries
dataset-uuid, checksum, sensitivity and depositor labels. Member files
upload first and the record is written last, so its presence marks a
complete deposit - an interrupted batch simply re-runs, skipping files
that already match the manifest. Depositing again to the same prefix loads
the existing record as defaults instead of re-asking everything, and adds
to the manifest (never removes; curation is a deliberate act done
elsewhere). Member filenames matching `dataset.*.json` are therefore
reserved.

Filenames are preserved exactly as deposited; if a name contains awkward
characters the tool offers a correction but never applies one silently.
After upload, every manifest entry is verified to exist at the expected
size before the deposit is reported done.

## Folders and sub-paths

A folder argument is walked, and each file's path **relative to that
argument** is preserved in both the object key and the manifest, instead
of being flattened to a bare filename:

```
python deposit.py survey-2024/ --dataset coastal ...

survey-2024/2024/tiles/a.tif  ->  .../coastal/2024/tiles/a.tif
survey-2024/readme.md         ->  .../coastal/readme.md
```

The folder's own name (`survey-2024`) is never part of the key - the
dataset element already names the collection. The same rule anchors a
glob that spans directories: `data/*/results.csv` lands each match
relative to `data`, so `data/site1/results.csv` and
`data/site2/results.csv` no longer collide the way two flat
same-named files would. An explicit single-file argument still lands at
just its basename, exactly as before. A trailing slash makes no
difference either way (`survey-2024` and `survey-2024/` are identical) -
tab-completion adds one inconsistently across shells, so it is not
treated as a signal. `**` now recurses to any depth (it previously
matched only one level - re-check any script that already uses it, since
it now covers more files than before). A literal filename containing `[`
or `]` (e.g. `data[1].csv`) is matched literally with a note, rather than
being read as a wildcard and reported as "no match".

Within a walked folder: OS/editor noise (`.DS_Store`, `Thumbs.db`,
`._*` AppleDouble files, `.git/`, `__pycache__/`, etc.) is excluded by
default and the count is reported - pass `--include-noise` to deposit
them anyway. Hidden/dotfiles ARE included (unlike a bare `*` glob, which
skips them - the tool says how many). Symlinked files are followed;
symlinked directories are not descended (to avoid cycles) and are
named. Empty folders contribute nothing, since object storage has no
directories. A member path matching the reserved `dataset.*.json`
pattern is refused at any depth, not just the last segment.

Re-depositing a file that already exists in the dataset under a
*different* path (e.g. it was flat before and is now inside a folder) is
detected by content and warned about before the point of no return:
deposits are additive and the tool never removes a manifest member, so
this would otherwise silently double the bytes in the dataset.

The abstract's guidance is at least 50 words; shorter ones warn but do not
block, which is deliberate for the testing phase — expect this to tighten
before real deposits begin. Red-classified data is refused — it belongs
in the TRE, not shared storage.

## Configuration

On first run the tool asks which rclone remote and bucket to use (picking
the remote from a menu of those you already have) and saves the answer to
`%LOCALAPPDATA%\crsw-deposit\config.json` (Windows) or
`~/.config/crsw-deposit/config.json` (macOS/Linux). Precedence is
command-line flag → saved config → built-in default. Run with
`--reconfigure` to change the saved values. Every run starts with a
`Target:` line showing the resolved `remote:bucket` and where each value
came from, so there is never any doubt about which storage a deposit used.

Passing `--remote`/`--bucket` on the command line is transient by
default - it applies to that run only. If the value differs from what's
saved (and the remote/bucket has just been proven reachable and
writable), the tool asks once whether to make it the new default; a
plain Enter declines. This prompt never appears under `--dry-run`, when
running non-interactively (piped/scripted input), or when nothing
actually changed.

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

## Platform notes

The tool runs unchanged on Windows, macOS and Linux (stdlib only,
`pathlib` throughout, object keys always use `/`). Config lives at
`~/.config/crsw-deposit/config.json` on macOS/Linux (`XDG_CONFIG_HOME`
is not consulted); the vocabulary cache and `deposits.log` live at
`~/.cache/crsw-deposit/` (`XDG_CACHE_HOME` likewise not consulted).
`$COMPUTERNAME`/registry lookups are never used - MIME types come from a
private table so `.csv` doesn't vary by the depositor's OS.

A few things worth knowing if you're depositing from macOS or Linux:

- **rclone must be executable.** A binary dropped next to the script (or
  downloaded manually) needs `chmod +x rclone`; a browser download on
  macOS may also carry a quarantine flag - clear it with
  `xattr -d com.apple.quarantine rclone` if the tool reports it can't be
  found.
- **Accented filenames are Unicode-normalised (NFC) in the object key
  and manifest** - macOS hands back decomposed (NFD) Unicode for the
  same characters Windows and Linux hand back composed, so without this
  the identical file deposited from a Mac and a PC would produce two
  manifest entries. Your local file is never renamed; only the object
  name and manifest path are normalised, and the tool says so when it
  changes anything.
- **The problem-character check (`: * ? " < > |`, and leading/trailing
  space) is about the file's *consumers*, not your filesystem** - those
  characters are legal on macOS/Linux but break a straight save on
  Windows, so they're still flagged even though your OS is happy with
  them.
- Two files that differ only by case (`README.md` / `readme.md`) are
  distinct on Linux and in object storage, but collide on Windows and
  default macOS - avoid depositing both into one dataset.

## Development

Run the tests with:

```
python -m unittest discover -s tests -v
```

`dataset.schema.json` is the contract for the record format, used by CI
and the future ingestion gateway; the shipped tool validates by hand
(standard library only). Tests that need the `jsonschema` package skip
cleanly when it is not installed. The Dublin Core mapping itself ships as
`crsw-dc-mapping.json` — the single source of truth, not a table in a
document — and `export_dcat.py` renders it into a DCAT (JSON-LD) dataset
description, doubling as the proof that every schema field is mapped or
explicitly marked local.

The build spec is `DEPOSIT_TOOL_SPEC.md`, revised by `DEPOSIT_TOOL_SPEC_r2.md`
(r2 supersedes where it speaks). Module boundaries and constraints are
documented there and in `CLAUDE.md`.
