# CRSW Deposit Tool — build spec

Command-line tool for depositing research files into the Centre's shared object storage (Ceph, S3-compatible, hosted by KCL eResearch). Applies path conventions and writes a sidecar metadata file per object.

**Status:** not yet built. This spec is the brief.
**Companion docs:** Data Governance Handbook v0.4 (§4 paths, §8.2 workflow), Sidecar Metadata Specification v0.2 (field reference).

---

## 1. Why this exists

The manual deposit workflow — construct a path, hand-write JSON, compute a checksum, run two rclone commands, per file — will not survive contact with ~50 researchers. A web ingestion gateway is planned but not built.

**This tool is a prototype of that gateway's back end.** Validation, field logic and key construction must be importable modules with no interactive I/O, so the gateway can later import them and put a web form in front. If it's written as one interactive blob it gets thrown away. This is the most important structural constraint here.

### Scope

In: green and amber deposits; single files and batches; sidecar generation; vocabulary validation; checksums; preflight checks; human-readable errors.

Out: red data (never enters shared storage — refuse it); download/sync; catalogue integration (no catalogue chosen yet); auth beyond rclone's own credentials; anything needing cluster admin rights.

---

## 2. Hard constraints

| Constraint | Detail |
|---|---|
| **Stdlib only** | No pip install, no venv, no requirements.txt. `hashlib`, `json`, `argparse`, `pathlib`, `subprocess`, `urllib.request`, `datetime`, `os`, `sys`, `tempfile` are sufficient. This is why the vocabulary is JSON not YAML. |
| **Python 3.8 floor** | No `dict \| dict` merge, no runtime `list[str]` / `dict[str, str]` annotations (use `typing.List` etc. or skip annotations), no walrus-dependent logic, no `str.removeprefix`. |
| **Cross-platform** | Windows / macOS / Linux. `pathlib` for local paths. Object keys **always** use `/` regardless of platform — never let `os.sep` leak into a key. |
| **rclone detected, not assumed** | Look on PATH, then `./rclone`, then `./rclone.exe`. If absent: print download URL + config stanza, exit cleanly. |
| **Single distributable** | Modules separate in dev, but shipped as one folder that copies and runs. Must stay PyInstaller-compatible (no dynamic imports, no `__file__` assumptions that break when frozen — use `sys._MEIPASS` fallback for the bundled vocab). |
| **No secrets handled** | Credentials live in rclone's config. Read it if needed; never write, log, or echo it. |

---

## 3. Module structure

```
deposit.py      CLI only: argparse, prompting, preview, confirmation,
                progress, error translation. Imports the others.

sidecar.py      Field definitions, tiering, validation, JSON assembly,
                checksum. NO user I/O. Pure functions where possible.
                ← this is what the gateway imports

keys.py         Path construction + filename checks. Isolated because
                the path convention may change; one function to rewrite.

vocab.json      Bundled fallback copy of the subjects vocabulary.
```

Keep `keys.py` separate from `sidecar.py`. They change for different reasons: the field list evolves as the catalogue firms up; the path convention evolves if governance revisits §4.

---

## 4. Path and filename rules

```
{strand}/{project}/{state}/{sensitivity}/{original-filename}

rs2/csac/2_final/green/csac-clean-2025.csv
rs2/csac/2_final/green/csac-clean-2025.csv.meta.json
```

**Filenames are preserved exactly as deposited.** This is a deliberate decision (handbook v0.4 §4.3) reversing an earlier convention that renamed files. Do not rename silently, ever.

Filename handling:
- Flag spaces and `\ / : * ? " < > |`. Offer a correction (spaces→hyphens, problem chars stripped), let the user accept or edit. Never auto-apply.
- Warn on collision with an existing key: explain it creates a *new version* of that object, not a second file. Require explicit confirmation.
- Sidecar key is always `{data_key}.meta.json`.

Controlled values:

| Field | Values |
|---|---|
| `strand` | `rs1` `rs2` `rs3` `rs4` |
| `domain` | `quant` `geo` `pol` `narr` `parti` |
| `state` | `0_raw` `1_interim` `2_final` |
| `sensitivity` | `green` `amber` (**`red` → refuse deposit**) |
| `project` | Open list. Offer existing prefixes from the bucket first; new names need explicit confirmation. Lowercase, hyphens. |
| `subjects` | Open, faceted, fetched at runtime (§6) |

The project rule matters: without it you get `csac`, `CSAC` and `csac-data` as three sibling directories, permanently.

---

## 5. Sidecar schema

Written to `{data_key}.meta.json`. Full reference in the Sidecar Metadata Specification v0.2; reproduced here so the build doesn't need it open.

### Required

| Field | Type | Notes |
|---|---|---|
| `schema_version` | str | `"0.2"` |
| `object_key` | str | Full key of the data file |
| `strand` | str | `rs1`–`rs4` |
| `domain` | str | `quant`/`geo`/`pol`/`narr`/`parti` |
| `project` | str | Second path element |
| `state` | str | `0_raw`/`1_interim`/`2_final` |
| `sensitivity` | str | `green`/`amber` |
| `coverage_start` | str | ISO date or year-only (`"1989"`) |
| `coverage_end` | str | ISO date or year-only |
| `version` | str | `v3-0` — sidecar is its only home now |
| `abstract` | str | 100–300 words. Warn outside that range, don't block. |
| `subjects` | list[str] | ≥1 term from the vocabulary |

### Recommended (populate automatically where possible)

| Field | Type | Notes |
|---|---|---|
| `vocabulary_version` | str | Date-stamp of the vocab list used |
| `source` | str | Origin: archive, survey, scrape, partner org |
| `depositor` | str | Institutional username |
| `deposited` | str | ISO 8601 UTC timestamp — auto |
| `checksum_sha256` | str | Auto. Never typed by hand. |
| `licence` | str | `CC-BY-4.0` etc., or `internal-only` for amber |
| `steward` | str | Domain steward name |

### Optional

`derived_from` (parent object key), `language` (ISO 639-1 list), `ethics_ref`, `notes`.

### Example

```json
{
  "schema_version": "0.2",
  "object_key": "rs2/csac/2_final/green/csac-clean-2025.csv",
  "strand": "rs2",
  "domain": "quant",
  "project": "csac",
  "state": "2_final",
  "sensitivity": "green",
  "coverage_start": "1989",
  "coverage_end": "2025-12-31",
  "version": "v3-0",
  "abstract": "Cleaned extract of the CSAC database covering coded records to the end of 2025. [100-300 words: content, coverage, known limitations]",
  "subjects": ["armed-conflict", "forced-labour", "human-trafficking"],
  "vocabulary_version": "2026-07-23",
  "source": "CSAC coding project, University of Nottingham",
  "depositor": "njakeman",
  "deposited": "2026-07-23T14:05:00Z",
  "checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "licence": "CC-BY-4.0",
  "steward": "Kevin Fahey",
  "derived_from": "rs2/csac/1_interim/amber/csac-raw-2025.csv"
}
```

Checksum: `hashlib.sha256`, read in chunks (64KB–1MB) so a 10GB raster doesn't load into memory.

---

## 6. Vocabulary handling

Subjects vocabulary lives in a GitHub repo, fetched at runtime so a strand owner's merged addition is live for everyone on their next deposit.

```json
{
  "vocabulary_version": "2026-07-23",
  "facets": {
    "practices": ["forced-labour", "sexual-slavery", "forced-marriage",
                  "child-soldiering", "debt-bondage", "domestic-servitude",
                  "forced-criminality", "human-trafficking", "forced-displacement"],
    "themes":    ["legal-frameworks", "prosecution", "prevalence", "prevention",
                  "survivor-experience", "reparations", "conflict-financing",
                  "supply-chains", "humanitarian-response", "post-conflict-transition"],
    "methods":   ["remote-sensing", "survey", "interview", "archival",
                  "legal-analysis", "osint", "participatory", "statistical-modelling"],
    "contexts":  ["armed-conflict", "occupation", "displacement-camps",
                  "extractive-industries", "agriculture", "construction", "maritime"]
  }
}
```

**This list is a strawman awaiting domain steward review.** Ship it as `vocab.json`, expect it to change.

Fetch logic:
1. HTTPS GET the raw file, timeout 3–5s. A slow network must never block a deposit.
2. On success: cache to `%LOCALAPPDATA%\crsw-deposit\` (Windows) or `~/.cache/crsw-deposit/` (else), use it.
3. On failure: fall back to cache → bundled `vocab.json`. Warn once, non-fatally, naming which source was used.
4. Record `vocabulary_version` in every sidecar.
5. Unknown terms are **refused** with instructions on proposing an addition. Not silently accepted, not silently dropped.

> Test early: some VPN configs route all traffic and may block GitHub. If KCL's does, the fallback chain is load-bearing rather than belt-and-braces.

---

## 7. CLI behaviour

```
python deposit.py FILE_OR_GLOB [FILE_OR_GLOB ...] [options]

  --strand rs2            skip strand prompt
  --project csac          skip project prompt
  --state 2_final         skip state prompt
  --sensitivity green     skip sensitivity prompt
  --dry-run               preview keys + sidecars, upload nothing
  --remote ceph           rclone remote name (default: ceph)
  --bucket crsw           target bucket
```

Anything not supplied is prompted for. Fully-flagged `--dry-run` is how researchers check before committing and how support reproduces a problem.

### Sequence

1. **Preflight** (§8). Fail early and legibly.
2. **Resolve file list.** Expand globs. Reject directories (no `--recursive` in v1). Report count + total size.
3. **Fetch vocabulary** (§6).
4. **Prompt batch metadata:** strand, project, state, sensitivity, domain, version, coverage dates, subjects, abstract.
5. **Offer per-file overrides once:** `Apply the same abstract and dates to all N files? [Y/n]`. Coverage dates are the most likely to differ.
6. **Check filenames** (§4). Offer corrections; warn on collisions.
7. **Preview.** Per file: the key it will occupy + sidecar summary. Show first 3 and a count if the batch is large.
8. **Confirm.** Single `y/N`. Last point of no return — make it look like it.
9. **Per file:** checksum → write sidecar to temp → `rclone copyto` data → `rclone copyto` sidecar → verify both.
10. **Report.** What succeeded, what failed, what to do next. Log line per deposit.

### Batch semantics

- Strand, project, state, sensitivity, domain, version, subjects, abstract shared across batch by default.
- Coverage dates shared by default but most likely to need overriding.
- **Not atomic.** If file 7 of 20 fails, 1–6 stay deposited. Report clearly which landed.
- **Re-running must be safe.** Keys are deterministic, so a re-run overwrites — bucket versioning makes that recoverable — but say so rather than doing it silently.

---

## 8. Preflight checks

This is where deposits actually fail, and where the difference between a tool people use and one they abandon gets made. Every failure needs a specific, actionable message.

| Check | Message must say |
|---|---|
| rclone present | Where it looked, download URL, that `./rclone` in cwd also works |
| Remote configured | Expected remote name + exact config stanza to paste, with key placeholders |
| Endpoint reachable | Storage unreachable, **KCL VPN is the usual cause**, check VPN first. Never print a raw socket error. |
| Credentials valid | Credentials rejected; contact eResearch if new or recently rotated |
| Write permission on prefix | Which prefix was refused; access is scoped by strand — an RS2 credential writing to `rs3/` is a permissions question, not a bug |
| Files readable | Which path failed |
| Sensitivity ≠ red | Red goes to the TRE. Point at handbook §3 and §7.3. Refuse outright. |

> Test: turn the VPN off and run it. If the output mentions sockets, TLS, or an S3 error code, it isn't finished.

---

## 9. Transfer

Use **`rclone copyto`**, never `rclone copy`. `copy` treats the destination as a directory and keeps the original name inside it; `copyto` places the object at exactly the given key. This has already caught us once.

```bash
rclone copyto "local/path/csac-clean-2025.csv" \
  ceph:crsw/rs2/csac/2_final/green/csac-clean-2025.csv

rclone copyto "tmp/csac-clean-2025.csv.meta.json" \
  ceph:crsw/rs2/csac/2_final/green/csac-clean-2025.csv.meta.json
```

- Data file first, sidecar second. An object briefly without a sidecar is recoverable; a sidecar pointing at nothing is confusing.
- Surface rclone progress for large files rather than appearing to hang (`--progress`, or parse stats output).
- Verify both objects exist (`rclone lsjson` on the two keys) before reporting success.
- Clean up temp sidecar files on both success and failure.

---

## 10. Test cases

Environment failures matter more than the happy path — they're what will actually happen.

| Case | Expected |
|---|---|
| Single small file | Both objects land; sidecar validates; key matches preview |
| Batch of 20 mixed files | Shared metadata applied; override offered once; correct count |
| VPN off | Plain-language message naming the VPN; no stack trace |
| Wrong/expired credentials | Points to eResearch; no raw S3 error |
| Write outside user's prefix scope | Explains strand scoping |
| rclone absent | Download URL + config stanza |
| GitHub unreachable | Falls back to cache/bundle, warns once, deposit completes |
| Filename with spaces + `&` | Correction offered, not auto-applied |
| Key already exists | Collision warning explaining versioning; explicit confirm |
| Unknown subject term | Refused with contribution instructions |
| `sensitivity=red` | Refused with TRE pointer |
| File >5GB | Chunked hashing doesn't exhaust memory; progress visible |
| Ctrl-C mid-batch | Reports what completed; re-run is safe; no orphan temp files |
| `--dry-run` | Full preview, nothing uploaded, no temp files left |

---

## 11. Pilot

First real user is Sima Farokhnejad (RS2): ~500MB structured data + text, three-person access. Small and clean — a poor stress test, a good design test.

1. Build against her deposit specifically.
2. Watch her use it **without helping**.
3. Every question she asks is either a missing prompt, an unclear label, or a field that shouldn't exist. Record all of them.
4. Revise the Sidecar Metadata Specification to v0.3 on that evidence *before* other strands touch it.
5. Then generalise: RS3's large rasters test the transfer path; RS1's legal texts test the vocabulary.

Expect the first version to be wrong about something. That's the point of piloting with one cooperative user rather than launching to fifty.

---

## 12. Blocked on (doesn't stop the build; does stop shipping)

- [ ] Starter subjects vocabulary reviewed by domain stewards (Katarina / Doreen / Kevin)
- [ ] GitHub repo created for vocabulary + permission matrix
- [ ] Confirm Python is present on managed KCL/UoN Windows machines — a negative answer changes distribution from "copy a file" to "sign and notarise PyInstaller binaries"
- [ ] Bucket name and rclone remote naming convention fixed with eResearch
- [ ] Decide whether the tool may create new project prefixes or only use existing ones

---

## 13. Build order

1. `keys.py` + tests — pure logic, no dependencies, fastest to verify
2. `sidecar.py` + tests — validation and assembly, still no I/O
3. `vocab.json` + fetch/cache/fallback chain
4. rclone detection and preflight checks
5. `deposit.py` prompting and preview
6. Transfer and verification
7. Error message pass — go back through every failure path and rewrite the message for someone who has never seen the code

Step 7 isn't polish. It's most of the difference between a tool that gets adopted and one that gets abandoned after a bad afternoon.
