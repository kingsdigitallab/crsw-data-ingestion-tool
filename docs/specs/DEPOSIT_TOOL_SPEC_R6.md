# CRSW Deposit Tool — revision spec (r6)

Dataset becomes a path element; the record takes the dataset's name; the Dublin Core mapping becomes a shipped artefact rather than a table in a Word document.

**Supersedes:** r5 §1 (Q1 boundary, Q2 record naming), r5 §2–3 (record format and disposition, as amended below), r3/r4 path rules. Everything else in r2–r5 stands.
**Origin:** design discussion 29–30 July. Dataset granularity settled: a project may hold several distinct datasets.
**Dependency:** unchanged from r5 §12 — partner endorsement of dataset-as-unit still outstanding. Build; don't roll out.

---

## 0. Check first: validation drift, and the abstract rule

A test record was accepted with `"abstract": "2"`, while the r5 schema sets `minLength: 50`. Either the hand-rolled runtime validation in `record.py` doesn't enforce what the schema declares, or the dev-dependency schema test (r5 §7) isn't wired up. The second is the mechanism that stops this recurring, so wire it up regardless.

**Resolution — the schema and the tool do different jobs:**

- **Schema: drop the `minLength` constraint on `abstract` entirely.** The schema encodes what is invariant. A length floor is editorial guidance, and a schema that contradicts what the tool accepts produces a permanently red CI test everyone learns to ignore — worse than no test.
- **Tool: warn below 50 words, never block.** Note the unit change: the old `minLength: 50` was fifty *characters*, which was a weak proxy for guidance expressed in words. The tool counts words.

Suggested wording:

```
  Abstract is 12 words. Guidance is at least 50 — enough for someone
  deciding whether this dataset is worth requesting. Continue anyway? [y/N]
```

Lenience is deliberate for the testing phase. It should not survive it: an abstract of "2" in a real record is a catalogue entry nobody can use. Flagged in §8 as a switch to reconsider before the pilot, not a decision taken once.

Resolve all of this before anything below, because the rest of this spec adds fields and constraints that would drift the same way.

---

## 1. Dataset as a path element

```
was:  {strand}/{project}/{sensitivity}/{state}/{original-filename}
now:  {strand}/{project}/{sensitivity}/{state}/{dataset}/{original-filename}

rs3/kilns/green/2_final/sentinel2-imagery/S2A_MSIL2A_20240101T.tif
rs3/kilns/green/2_final/training-labels/labels_kilns_v1.geojson
```

**Why depth rather than several records in one flat prefix:** it removes the filename-collision hazard entirely (two datasets can each have a `readme.md`), and it keeps dataset deletion as `rclone purge <prefix>` rather than a manifest-driven iteration — which matters for the lifecycle work flagged as G3 in the gap analysis. The cost, accepted: the extra level exists even when a project holds one dataset, and a file can no longer belong to two datasets at once.

Access control is unaffected. `rs3/kilns/green/*` still covers everything beneath it, so existing policy patterns and the sensitivity-contiguity rationale from r3 are untouched. No mid-pattern wildcard is required for this change.

### Dataset picker

Mirrors the r4 project picker exactly, one level down. After state selection:

```
Datasets in rs3/kilns/green/2_final:
  [1] sentinel2-imagery
  [2] training-labels

  [n] new dataset
> 1
```

- Listing: `rclone lsf {remote}:{bucket}/{strand}/{project}/{sensitivity}/{state}/ --dirs-only`
- Same normalisation as projects (lowercase, hyphens, strip anything outside `[a-z0-9-]`), same explicit confirmation for new names, same near-match warning.
- Same permissions-artefact fallback: if the listing fails or returns empty on a prefix the user can write to, fall back to free-text entry with an honest note rather than asserting "no datasets here".
- Empty state → straight to the new-dataset prompt with a note.
- `--dataset` flag skips the prompt; an unknown value is treated as a creation and confirmed once.

Prompt order becomes strand → project → sensitivity → state → dataset. Reuse the picker helper; this should be a parameterised call, not a second implementation.

---

## 2. Record naming: `dataset.<slug>.json`

The record file takes the dataset's name rather than a fixed `dataset.meta.json`:

```
rs3/kilns/green/2_final/sentinel2-imagery/dataset.sentinel2-imagery.json
```

The slug always equals the final path element, so the key stays deterministic — any consumer builds it as `{prefix}/dataset.{final-element}.json` with no listing required. **Validation rule:** record filename must match the containing prefix's final element; a mismatch is an error, not a warning.

Rationale is the travel case. Download three datasets for offline work, or send a record to a colleague, and three files called `dataset.meta.json` collide and identify nothing. The `dataset.` prefix stays so that the file is recognisably a record out of context, and so the tool has a clean reservation pattern.

**Reservation changes from a name to a pattern:** member files matching `dataset.*.json` are refused at `keys.py`.

### Identifier

Back to a plain prefix string — the `#` fragment form discussed earlier is dropped, since the dataset is now a real path element:

```
"identifier": "rs3/kilns/green/2_final/sentinel2-imagery"
```

`dataset_uuid` is retained and still load-bearing: the prefix changes under reclassification and state promotion, the UUID does not.

---

## 3. Dublin Core mapping, made explicit

Two changes: rename the fields where the standard term is unambiguous, and ship the mapping as a machine-readable artefact so it travels with the code instead of sitting in a document.

### 3.1 Renames

| v0.4 field | v0.5 field | Term |
|---|---|---|
| `subjects` | `subject` | `dcterms:subject` (DC term is singular; array value is fine) |
| `licence` | `license` | `dcterms:license` — US spelling is the term, irritating but correct |
| `coverage_start` / `coverage_end` | `temporal: {"start": ..., "end": ...}` | `dcterms:temporal`, structured as DCAT expects rather than two flat keys |

Per-file coverage in manifest entries takes the same nested `temporal` shape, for consistency.

Fields already carrying DC-term names stay as they are: `abstract`, `created`, `modified`, `creator`, `identifier`, `language`, `format`.

### 3.2 Fields that deliberately keep local names

Renaming these would assert semantics we don't mean:

- `source_type` / `source_detail` → maps to `dcterms:provenance` (acquisition narrative). **Not** `dcterms:source`, which means derivation. Calling it `source` would actively mislead.
- `derived_from` → `dcterms:source` / `prov:wasDerivedFrom` on export. Local name is clearer in situ.
- `sensitivity` → `dcterms:accessRights`, but `green`/`amber` are not DC-shaped values; the mapping handles the translation.
- `version`, `checksum_sha256`, `schema_version`, `strand`, `project`, `state`, `domain`, `vocabulary_version`, `steward`, `bytes`, `ethics_ref` → local. No standard term, or none worth the false precision.
- `depositors` → `dcterms:contributor` on export; the local name is more precise about what it records.

### 3.3 The mapping artefact

Ship `crsw-dc-mapping.json` in the repo alongside `dataset.schema.json`, governed by the same `schema_version`:

```json
{
  "schema_version": "0.5",
  "namespaces": {
    "dcterms": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
    "prov": "http://www.w3.org/ns/prov#",
    "crsw": "[placeholder — Centre namespace URI to be decided]"
  },
  "dataset_fields": {
    "abstract":    {"term": "dcterms:description"},
    "subject":     {"term": "dcterms:subject"},
    "temporal":    {"term": "dcterms:temporal", "note": "start/end become dcat:startDate/dcat:endDate"},
    "license":     {"term": "dcterms:license", "note": "identifier values only; internal-only maps to dcterms:rights"},
    "sensitivity": {"term": "dcterms:accessRights", "values": {"green": "public", "amber": "restricted"}},
    "creator":     {"term": "dcterms:creator"},
    "depositors":  {"term": "dcterms:contributor"},
    "created":     {"term": "dcterms:dateSubmitted"},
    "modified":    {"term": "dcterms:modified"},
    "identifier":  {"term": "dcterms:identifier"},
    "language":    {"term": "dcterms:language"},
    "spatial":     {"term": "dcterms:spatial"},
    "source_type": {"term": "dcterms:provenance", "note": "acquisition, not derivation"},
    "source_detail": {"term": "dcterms:provenance"},
    "derived_from":  {"term": "dcterms:source", "also": "prov:wasDerivedFrom"},
    "strand":      {"term": "crsw:strand", "local": true},
    "project":     {"term": "crsw:project", "local": true},
    "state":       {"term": "crsw:state", "local": true},
    "domain":      {"term": "crsw:domain", "local": true},
    "version":     {"term": "crsw:version", "local": true},
    "steward":     {"term": "crsw:steward", "local": true},
    "schema_version":     {"term": "crsw:schemaVersion", "local": true},
    "vocabulary_version": {"term": "crsw:vocabularyVersion", "local": true},
    "ethics_ref":  {"term": "crsw:ethicsRef", "local": true},
    "dataset_uuid": {"term": "dcterms:identifier", "note": "stable identifier; identifier field is the readable form"}
  },
  "file_fields": {
    "path":            {"term": "dcat:downloadURL", "note": "relative to dataset prefix"},
    "format":          {"term": "dcterms:format"},
    "bytes":           {"term": "dcat:byteSize"},
    "checksum_sha256": {"term": "spdx:checksum", "local": true, "note": "named for kinship; SPDX not imported"},
    "temporal":        {"term": "dcterms:temporal"}
  }
}
```

**`export_dcat.py` reads this file rather than hardcoding the mapping**, which makes the mapping the single source of truth and the converter a thin renderer. A field present in the schema but absent from the mapping is a build error — that check is what keeps the two in step as fields are added.

The Centre namespace URI is a placeholder; it needs a real value before anything is published externally, and it's a Centre decision rather than a technical one.

### 3.4 Deliberately not JSON-LD

Records stay plain JSON with no `@context`. The interoperability claim is carried by the mapping file plus a working converter, which is demonstrable, rather than by embedded ceremony no current consumer reads. If the catalogue platform turns out to want JSON-LD, the mapping file makes that a short step.

---

## 4. Schema changes (`dataset.schema.json` → 0.5)

- `schema_version`: `"0.5"`
- `identifier` pattern gains the fifth segment: `^rs[1-4]/[a-z0-9-]+/(green|amber)/(0_raw|1_interim|2_final)/[a-z0-9-]+$`
- New required `dataset` (the slug), pattern `^[a-z0-9-]+$`
- `subjects` → `subject`; `licence` → `license`
- `abstract`: `minLength` removed (see §0) — length guidance lives in the tool, not the contract
- `coverage_start`/`coverage_end` → `temporal` object with required `start` and `end`
- `files[].path`: the `dataset.meta.json` exclusion becomes a pattern exclusion for `dataset.*.json`
- `files[]` per-file coverage takes the nested `temporal` shape

---

## 5. Code touch-points

| Module | Change |
|---|---|
| `keys.py` | Fifth path element; record key = `{prefix}/dataset.{slug}.json`; reserved *pattern* check; slug-matches-prefix validation |
| `record.py` | Field renames; `temporal` nesting; envelope computation over nested shape; `dataset` field |
| `deposit.py` | Dataset picker (parameterised reuse of the project picker); prompt order; existing-record load now keys on the dataset prefix |
| `dataset.schema.json` | §4 |
| `crsw-dc-mapping.json` | New, §3.3 |
| `export_dcat.py` | Read the mapping file instead of hardcoded terms; fail on unmapped fields |

---

## 6. Tests (extending r5 §10)

| Case | Expected |
|---|---|
| Project with two datasets, same `readme.md` in each | Both deposit cleanly, no collision |
| New dataset in existing project | Picker offers existing, creates on confirmation |
| Near-match dataset name (`sentinel2` vs `sentinel2-imagery`) | Warning before confirmation |
| Record filename vs prefix mismatch (hand-edited) | Refused with a clear message |
| Member file named `dataset.foo.json` | Refused at `keys.py` |
| Deterministic record key | Consumer builds key from prefix alone, no listing |
| `temporal` envelope with per-file overrides | Envelope computed; containment enforced |
| Schema field absent from mapping file | Build error from `export_dcat.py` |
| `export_dcat.py` on worked example | Every field mapped or flagged local; valid DCAT |
| Abstract under 50 words | Warned with a word count, not refused; schema carries no length constraint |
| Emitted test record vs schema | Passes — no contradiction between tool lenience and contract |

---

## 7. Migration

Existing test deposits sit at v0.4-style prefixes with `dataset.meta.json`. They are test material: **purge rather than migrate**.

```
rclone purge s3_kcl_neil:neil-test-01/rs1
```

If any real deposit exists by the time this lands, the migration is a prefix move plus a record rename plus the field renames — extend `migrate_v03.py` rather than writing a second script. Check before assuming; the window for "just purge it" is short.

---

## 8. Carried over — agreed or flagged earlier, not yet built

These came up in discussion and haven't landed in a spec. Listed so they stop being remembered ad hoc:

- **Restricted-project deposit guard.** From the permissions discussion: when a user creates a new project or dataset, ask whether access will be restricted beyond the strand. If yes, stop and explain the matrix-first procedure — policies applied *before* first deposit, never deposit-then-restrict. The tool can't verify the denies exist, but it can make the safe path the described one. Sima's pilot is the first restricted case.
- **Abstract guidance before the pilot.** The soft 50-word warning is right for testing. Decide before real deposits whether it stays advisory or becomes a block — and note that the handbook figure (§9) and the tool warning must agree whichever way it goes.
- **rclone header-upload round-trip.** r5 §6 flagged verifying that the installed rclone passes `x-amz-meta-*` through to Ceph, read back with `rclone lsjson --metadata`. If this has been confirmed, record the working rclone version in the preflight; if not, it blocks the object-label design.
- **`verifying...` hardening.** r2 §0: confirm the step queries the written key and compares size, rather than trusting `copyto`'s exit code. A false success is worse than a crash.
- **Manifest split at scale — decision needed.** The `files` array inside the record is comfortable at forty entries and not at RS3's thousands of raster tiles: the record stops being readable off the cluster, and a harvester pulls megabytes to read a 300-word abstract. Recommendation: keep one file, add a threshold above which the manifest splits to a sibling `dataset.<slug>.files.json`. Not built here — it wants RS3's real shape first.
- **Member removal semantics.** Still out of scope; deposits remain additive. Manual meanwhile.
- **`creator` guidance text.** Placeholder stands. Whether it means the PI, the strand, or the coding team is a Centre call.
- **Sysadmin conversation, still open from r3 §6.** Whether sensitivity should sit above project (`rs2/amber/csac/...`) for strand-wide policy grants. Unaffected by this revision, but it is the last outstanding path question and wants closing in the same conversation as the mid-pattern wildcard test.
- **Subjects vocabulary steward review**; **`ethics_ref` placement**; **catalogue platform** — unchanged, all upstream of the tool.

---

## 9. Documentation debt

Still batched, still deliberately not done in this pass. The batch is now larger and should land as one set once the build survives the pilot:

| Document | Change |
|---|---|
| `06` Sidecar spec → **v0.5** | Dataset path element; record naming; field renames; the DC mapping section becomes a pointer to the shipped artefact rather than a table; abstract guidance 100–300 words → **minimum 50 words** (§3.1 field table) |
| `02` Handbook → **v0.6** | §4.2 hierarchy gains the dataset level; §4.6 vocabularies gain dataset names; §8.2 workflow; §8.3 example; appendix. Abstract guidance 100–300 words → **minimum 50 words** (§6.4 form-field table). Plus the known stale fragments (`schema_version: "1.0"`, `data_date`) |
| `03` Recap → **v1.4** | §7–8 path and examples; fix "Only major versions appear in filenames"; decision table gains dataset-as-unit, dataset path element, record naming |
| `00` Register → **v1.5** | Register the above; add a row for the r-series specs as tool-repo artefacts |
| `04` Gap analysis | Check G3 (lifecycle) reads correctly now that dataset deletion is a prefix operation |

---

## 10. Build order

1. §0 validation drift — drop schema `minLength`, add the word-count warning, wire the schema test
2. `keys.py`: path element, record key, reserved pattern, slug validation
3. `record.py`: renames and `temporal` nesting
4. Schema 0.5 + dev-dep test
5. `crsw-dc-mapping.json` + `export_dcat.py` reading it
6. Dataset picker in `deposit.py`
7. Restricted-project guard (§8, first item)
8. Purge test data; re-deposit against the new shape
9. Error-message pass, last as always
