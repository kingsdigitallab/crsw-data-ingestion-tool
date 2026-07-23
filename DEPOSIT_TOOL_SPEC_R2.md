# CRSW Deposit Tool — revision spec (r2)

Changes to the working tool following first live testing against `s3_kcl_neil:neil-test-01`.

**Supersedes:** relevant sections of `DEPOSIT_TOOL_SPEC.md`. Everything not mentioned here stands.
**Context:** preflight, key construction, transfer and verification all work. This round is input ergonomics, one schema change, and one thing to check.

---

## 0. Non-cosmetic item first: verify what `verifying...` verifies

During testing a deposit reported `done:` for keys that weren't visible on a mounted drive. That turned out to be an `rclone mount` directory-cache artefact, not a failed upload — objects were present. No bug.

But it's worth confirming the verification step is real rather than decorative:

- **Real:** `rclone lsjson` (or `lsf`) against the exact key just written, checking the object exists and — better — that its size matches the local file.
- **Not enough:** trusting `copyto`'s exit code alone. A zero exit means rclone ran without error, not that the object landed where you meant.

If it's currently the weaker form, upgrade it. A false `done:` is worse than a crash, because nobody investigates a success.

While there: size comparison is cheap and catches truncated uploads. Comparing checksums against the object's ETag is *not* reliable — for multipart uploads the ETag is a hash-of-hashes and won't match a plain SHA-256. Don't attempt it.

---

## 1. Numbered quick-select for all closed vocabularies

Applies to **strand**, **state**, **domain**, and **sensitivity**. Accept either the number or the literal value — muscle memory and copy-paste should both work.

```
Strand:
  [1] rs1    [2] rs2    [3] rs3    [4] rs4
> 2

State:
  [0] 0_raw       raw, as received
  [1] 1_interim   in progress
  [2] 2_final     released or shared
> 2

Sensitivity:
  [1] green   publicly shareable
  [2] amber   internal, strand-scoped
> 1
```

Rules:

- Input `2`, `rs2`, and `RS2` all resolve to `rs2`. Case-insensitive throughout.
- State numbers map to the ordinal, not string position: `0` → `0_raw`, `1` → `1_interim`, `2` → `2_final`.
- Invalid input re-prompts with the list redisplayed — never silently defaults.
- Where a `--flag` supplied the value, skip the prompt entirely (existing behaviour).
- Short one-line hints as above where they aid the choice; skip them for strand, which needs none.

---

## 2. Domain becomes a fetched, extensible list

Domain currently sits in the handbook (§2.2) as five values, each with a named data steward. That list is still provisional — the handbook is an unratified draft — so the tool shouldn't hardcode it.

Move it into the version-controlled vocabulary file alongside subjects, with the same fetch → cache → bundled-fallback chain.

```json
{
  "vocabulary_version": "2026-07-23",
  "domains": [
    {"code": "quant", "label": "Quantitative conflict data", "steward": "Kevin Fahey"},
    {"code": "geo",   "label": "Geospatial & Earth observation", "steward": "Doreen Boyd"},
    {"code": "pol",   "label": "Legal & policy texts", "steward": "Katarina Schwarz"},
    {"code": "narr",  "label": "Survivor narratives & qualitative", "steward": "TBC"},
    {"code": "parti", "label": "Visual & participatory", "steward": "TBC"}
  ],
  "facets": { "practices": [...], "themes": [...], "methods": [...], "contexts": [...] }
}
```

Behaviour:

- Numbered select, displaying `code` and `label`.
- Store `code` in the sidecar (unchanged: `"domain": "quant"`).
- If the selected domain has a `steward`, auto-populate the sidecar `steward` field with it and show what it filled in. Saves a prompt and keeps steward attribution consistent.
- Unknown domain via `--domain` flag → refuse, list valid codes.

> Domains carry governance weight — each is meant to have a steward accountable for classification and quality decisions. Additions should go through the same proposal route as subjects, not be invented at deposit time. The tool doesn't need to enforce that beyond refusing unknown values; the vocabulary repo's review process does the rest.

---

## 3. Version format: drop the `v`

Store as `3-0`, not `v3-0`.

- Accept `3-0`, `v3-0`, `3.0`, `v3.0` on input. Normalise to `{major}-{minor}` for storage.
- Reject anything that isn't two integers separated by `-` or `.` — a bare `3` should re-prompt rather than being guessed as `3-0`.
- Default offered at the prompt: `1-0`.

**This is a documentation change too.** Sidecar Metadata Specification §3.1 and handbook §4.4 both show `v3-0`. Both need updating to match, in the same pass — otherwise the docs and the tool disagree, which is exactly the drift the document register exists to prevent.

---

## 4. Subjects: list before asking, and use colour

Currently the vocabulary is fetched but the terms aren't shown before the prompt, so depositors are guessing.

Display grouped by facet, numbered continuously across groups so selection is unambiguous:

```
Subjects — select one or more (comma-separated numbers or terms)

  practices
    [ 1] forced-labour          [ 2] sexual-slavery
    [ 3] forced-marriage        [ 4] child-soldiering
    [ 5] debt-bondage           [ 6] domestic-servitude
    [ 7] forced-criminality     [ 8] human-trafficking
    [ 9] forced-displacement

  themes
    [10] legal-frameworks       [11] prosecution
    ...

> 1, 8, armed-conflict
```

Rules:

- Accept numbers, literal terms, or a mix, comma-separated.
- Unknown term → refuse, name the offending term, show how to propose an addition. Do not silently drop it.
- At least one required.
- Echo the resolved list back before proceeding.
- Two-column layout if terminal width allows (`shutil.get_terminal_size()`); single column below ~100 chars.

### Colour

ANSI escapes, stdlib only:

- Windows: enable virtual terminal processing once at startup via `ctypes` (`SetConsoleMode` with `ENABLE_VIRTUAL_TERMINAL_PROCESSING`, 0x0004). Works on Windows 10+; harmless no-op elsewhere. Avoids a `colorama` dependency.
- Honour `NO_COLOR` (any value → disable) and `FORCE_COLOR`.
- Disable when `not sys.stdout.isatty()` so piped or redirected output stays clean.
- Wrap everything in one `style()` helper that returns plain text when colour is off. No raw escape codes scattered through the code.

Palette — restrained, semantic, not decorative:

| Use | Colour |
|---|---|
| Facet headings | bold |
| Selection numbers | dim |
| Prompt line | bold |
| Warnings (fallback vocab, filename correction, collision) | yellow |
| Errors and refusals | red |
| Success (`done:`, final summary) | green |
| Object keys in preview | cyan |

Colour must never be the only signal — anything colour conveys must also be in the words, for accessibility and for logs.

---

## 5. Source: structured type + free-text detail

Currently free text, which won't aggregate. Split into two fields.

```
Source type:
  [1] archive     existing collection or repository
  [2] survey      primary data collection instrument
  [3] scrape      automated extraction from an online source
  [4] instrument  sensor, satellite, or other device output
  [5] partner     supplied by a partner organisation
  [6] derived     produced from other data already held
  [7] other       none of the above
> 1

Source detail (free text, e.g. name, URL, or collection reference):
> ICRC archives, Geneva — conflict casualty records 1990-2005
```

Sidecar fields:

```json
"source_type": "archive",
"source_detail": "ICRC archives, Geneva — conflict casualty records 1990-2005"
```

Notes:

- `source_type` is a closed list, in the tool not the vocabulary file — it describes *how data was obtained*, which is stable, unlike subjects.
- `source_detail` is required and free text. Warn (don't block) if under ~10 characters — `"web"` helps nobody in five years.
- `other` prompts for detail with a nudge to be specific.
- `derived` should suggest also filling `derived_from` with the parent object key, if the depositor knows it.
- **Schema change:** replaces the single `source` field in Sidecar Metadata Specification §3.2. Both fields recommended tier. Needs a spec bump to v0.3 alongside the version-format change in §3 above.

---

## 6. Config file for remote and bucket

From testing: the built-in defaults (`ceph`, `crsw`) matched nothing, and both had to be passed as flags every run.

- On first run with no config, prompt for remote and bucket. Offer `rclone listremotes` output as a numbered menu rather than asking for free text — the remote almost certainly already exists.
- Write to `%LOCALAPPDATA%\crsw-deposit\config.json` (Windows) or `~/.config/crsw-deposit/config.json` (else), alongside the vocabulary cache.
- Precedence: command-line flag → config file → built-in default.
- `--reconfigure` re-runs the setup prompts.
- The "no remote named X" preflight error should **list the remotes that do exist**. The tool already knows them; a dead end becomes a menu.

---

## 7. Summary of changes to other documents

Three of the above alter documented conventions. They should move together as one pass, not trickle:

| Change | Affects |
|---|---|
| Version format `v3-0` → `3-0` | Sidecar spec §3.1; Handbook §4.4 |
| `source` → `source_type` + `source_detail` | Sidecar spec §3.2 |
| Domain becomes a fetched vocabulary | Sidecar spec §3.1 note; Handbook §4.6; possibly §2.2 |

Target: Sidecar Metadata Specification v0.3, Handbook v0.5. Hold until after the Sima pilot — she'll likely surface more, and one revision beats three.

---

## 8. Build order

1. `style()` helper + Windows VT enable — everything else uses it
2. Numbered select helper (one function, reused for strand/state/domain/sensitivity/source_type)
3. Domain into the vocabulary file, with steward auto-populate
4. Version normalisation
5. Subjects display + selection
6. Source split
7. Config file + `listremotes` in the preflight error
8. Verification hardening (§0)

Then a pass with the terminal at 80 columns and colour disabled, confirming nothing depends on width or colour to be legible.
