# CRSW Deposit Tool — revision spec (r5)

Dataset-level metadata, deposit manifest, and Dublin Core alignment. The major revision: the unit of description moves from the file to the dataset.

**Supersedes:** `DEPOSIT_TOOL_SPEC.md` §5 (sidecar schema) and the per-file sidecar model throughout; parts of §4.2 sequence. r2–r4 stand except where named.
**Origin:** design brief of 29 July (dataset record + manifest + standards alignment, treated as one revision). Q1–Q6 from that brief are resolved below as recommendations — the reasoning travels with the decision so any of them can be reopened deliberately rather than drifted past.
**Dependency:** dataset-as-unit is Centre-facing and partner endorsement was sought at the 29 July meeting. **Build now; do not ship to researchers until endorsement is confirmed** (see §12).

---

## 0. The shape of the change

The tool's batch was already a dataset in embryo: everything except coverage dates and checksums is collected once and duplicated into every per-file sidecar. This revision promotes the batch to the unit of record:

- **One `dataset.meta.json` per dataset**, at the dataset prefix root, holding the shared descriptive fields *and* a `files` manifest array.
- **Per-file sidecars are dropped.** A minimal travelling thread moves to S3 object metadata labels set at upload.
- **The record is written last**; its presence marks a deposit complete, which gives interrupted-batch recovery a natural semantics.
- **Descriptive keys align with Dublin Core** via a documented mapping, in plain JSON — catalogue (DCAT) and archival (RO-Crate) exports become mechanical conversions, provable by a small converter.

If a proposed structure below doesn't fall naturally out of what the tool already gathers, treat that as a design smell and query it.

---

## 1. Design decisions (Q1–Q6 resolved)

### Q1 — Dataset boundary: the prefix is the dataset

**Decision: (a).** A dataset is a project at a given sensitivity and state. Its record lives at:

```
{strand}/{project}/{sensitivity}/{state}/dataset.meta.json
```

A new deposit to that prefix updates the record and extends the manifest. A project receiving files monthly is one dataset gaining members.

Why not the others:
- **(b) dataset = deposit event** fragments identity: thirty uploads become thirty "datasets", the catalogue fills with events nobody would cite, and a re-run after an interruption would mint a spurious new dataset. Rejected.
- **(c) dataset = project + version** puts version inside identity, so the continuing thing ("the CSAC dataset") has no stable record — each increment orphans the last. Version belongs *in* the record, not around it. Rejected.

Consequence accepted knowingly: `rs2/csac/green/2_final` and `rs2/csac/amber/1_interim` are **two dataset records**. That is honest — they differ in exactly the dimensions (access, egress, citability) that matter — and dataset-level `derived_from` links them (§4).

Identity has two forms:
- `identifier` — the prefix string (`rs2/csac/green/2_final`). Human-readable, derivable, free.
- `dataset_uuid` — a UUID4 minted at first record creation (stdlib `uuid`), **stable across moves and reclassification**, which the prefix identifier is not. This is what object labels and the future catalogue key on.

### Q2 — Per-file sidecars: dropped

**Decision: (a).** No per-file metadata files. The sidecar's three jobs are covered:

- *Catalogue rebuild* and *direct cluster inspection*: the dataset record serves both **better** — one document to read, not forty.
- *Out-of-context travel*: object labels (`x-amz-meta-*`) carry a minimal thread on every object — `dataset-uuid`, `checksum-sha256`, `sensitivity`, `depositor` — set at upload time via rclone header flags (§6), no extra API calls. Someone copying the whole prefix through a mount gets record + manifest + files, which is *more* metadata than the old model gave them. The remaining gap — a single file copied out bare, since labels don't survive download — is already covered by handbook §4.3 doctrine (share by pointing, not attaching). Accepted.

Why not the others:
- **(c) full per-file sidecars generated from the record** is the status quo and is rejected explicitly: at forty files a deposit it means forty near-identical objects, and every description edit becomes a 41-object rewrite. The duplication that motivated this whole revision, kept.
- **(b) thin stubs** halve (c)'s content but keep its object count and add a second synchronisation surface, for marginal value only in the bare-single-file case that procedure already covers. Rejected.

Consequence: the `.meta.json` *suffix* convention retires. One name is reserved instead: a member file named exactly `dataset.meta.json` is refused at deposit (`keys.py` check).

### Q3 — Manifest form: one file

**Decision: single `dataset.meta.json`** containing both the descriptive fields and the `files` array. One object whose presence means "complete" and whose content is the whole story is simpler for the tool, for a researcher reading the cluster directly, and for the copy-the-folder travel case. The changes-often/changes-rarely argument for a separate manifest is real but its benefit (smaller diffs) is trivial under bucket versioning. Manifest entries borrow the Frictionless Data pattern (`path` / hash / bytes) with our explicit checksum key.

### Q4 — Write order and completeness

**Members first, record last.** The record's presence at the prefix flags a complete deposit.

- **Interrupted deposit:** member objects present, record absent (first deposit) or stale (subsequent) → incomplete by definition. No cleanup needed.
- **Re-run:** deterministic keys make it idempotent. The tool lists the prefix, loads any existing record, computes the union of existing manifest entries and the current batch, uploads missing or changed members, rewrites the record last.
- **Additive only:** a deposit adds members; it never silently removes them. Removing a member from a dataset is a deliberate curatorial act and is out of scope for r5 — manual for now, flagged open (§12).
- **Verification hardening** (extends r2 §0): after writing the record, re-read it, then confirm every manifest entry exists at its key with matching size. This replaces per-file verification as the completion check.

### Q5 — Per-file coverage: envelope at dataset level, overrides in the manifest

The record's `coverage_start`/`coverage_end` is the **envelope** of its members. Manifest entries *may* carry per-file coverage where it varies; where any do, the tool computes the envelope rather than asking for it, and validation requires the envelope to contain every member's range. Maps to `dcterms:temporal` at dataset level.

### Q6 — Dataset versioning

`version` is an attribute of the record (format `3-0`, per r2). Semantics:

- **Adding members does not bump the version by default.** The tool offers the current version as the default on subsequent deposits; incrementing is an explicit choice, per the handbook §4.4 criteria (schema change, significant coverage change, draft→released).
- **The manifest is a snapshot, not a delta.** Version 2-0's `files` array lists all current members, including those unchanged since 1-0.
- **History is free:** every rewrite of `dataset.meta.json` is preserved by bucket versioning, so the record's own version chain *is* the dataset's changelog. No separate version records, no `isVersionOf` in the common case.
- Where a genuinely separate record exists (a 2_final dataset promoted from 1_interim), dataset-level `derived_from` carries the link.

---

## 2. The record format

Plain JSON, snake_case keys, standard alignment carried by the mapping table (§3) rather than by JSON-LD ceremony. Rationale: the stdlib constraint, readability straight off the cluster, and the absence (yet) of any consumer that wants `@context`. The claim that DCAT/RO-Crate export is mechanical is *proven* by a converter (§9), not asserted.

### Worked example — CSAC, forty-file deposit

At `rs2/csac/green/2_final/dataset.meta.json`:

```json
{
  "schema_version": "0.4",
  "dataset_uuid": "8f14e45f-ceea-467f-a34e-9db1c153f0a1",
  "identifier": "rs2/csac/green/2_final",
  "strand": "rs2",
  "project": "csac",
  "sensitivity": "green",
  "state": "2_final",
  "domain": "quant",
  "version": "3-0",
  "abstract": "Cleaned extract of the CSAC database covering coded records to the end of 2025. [100-300 words: content, coverage, known limitations]",
  "subjects": ["armed-conflict", "forced-labour", "human-trafficking"],
  "vocabulary_version": "2026-07-23",
  "coverage_start": "1989",
  "coverage_end": "2025-12-31",
  "creator": "[person or team intellectually responsible — see field notes]",
  "source_type": "archive",
  "source_detail": "CSAC coding project, University of Nottingham",
  "licence": "CC-BY-4.0",
  "steward": "Kevin Fahey",
  "depositors": ["njakeman"],
  "created": "2026-07-29T10:15:00Z",
  "modified": "2026-07-29T10:15:00Z",
  "derived_from": "rs2/csac/amber/1_interim",
  "files": [
    {
      "path": "csac-clean-2025.csv",
      "checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "bytes": 48211023,
      "format": "text/csv"
    },
    {
      "path": "csac-codebook.pdf",
      "checksum_sha256": "[64-char hash]",
      "bytes": 1204880,
      "format": "application/pdf"
    },
    {
      "path": "csac-annual-1989.csv",
      "checksum_sha256": "[64-char hash]",
      "bytes": 220144,
      "coverage_start": "1989-01-01",
      "coverage_end": "1989-12-31",
      "format": "text/csv"
    }
  ]
}
```

(37 further entries elided. `format` from `mimetypes.guess_type`, omitted when unknown rather than guessed.)

### Object labels

Set on every member at upload:

```
x-amz-meta-dataset-uuid:    8f14e45f-...
x-amz-meta-checksum-sha256: e3b0c442...
x-amz-meta-sensitivity:     green
x-amz-meta-depositor:       njakeman
```

---

## 3. Field disposition table

Every v0.3 field, where it lands, and its standard term. Local-namespace fields are honest about being local.

| v0.3 field | Now | Standard term |
|---|---|---|
| `schema_version` | record, required | local |
| `object_key` | **dropped** — manifest `path` + record `identifier` serve | — |
| `strand` | record, required | local |
| `domain` | record, required | local (vocabulary-sourced, steward auto-fill stands per r2) |
| `project` | record, required | local |
| `state` | record, required | local |
| `sensitivity` | record, required + object label | `dcterms:accessRights` |
| `coverage_start` / `coverage_end` | record (envelope), required; manifest entry, optional | `dcterms:temporal` |
| `version` | record, required | local (`dcterms:isVersionOf` unused in common case — §1 Q6) |
| `abstract` | record, required | `dcterms:description` |
| `subjects` | record, required | `dcterms:subject` |
| `vocabulary_version` | record, recommended | local |
| `source_type` / `source_detail` | record, recommended | `dcterms:provenance` (acquisition narrative). **Not** `dcterms:source`, which means derivation — do not blur them |
| `depositor` | → `depositors` (list, appended per deposit), recommended; label carries the latest | `dcterms:contributor` |
| `deposited` | → `created` (first) + `modified` (every rewrite), auto | `dcterms:dateSubmitted` / `dcterms:modified` |
| `checksum_sha256` | manifest entry, required + object label | local (kin to DCAT's `spdx:checksum`, without the ceremony) |
| `licence` | record, recommended | `dcterms:license` for identifiers (CC-BY-4.0); amber's `internal-only` maps to `dcterms:rights` in export |
| `steward` | record, recommended | local |
| `derived_from` | record, optional (dataset-level); manifest entry, optional for rare per-file lineage | `dcterms:source` / `prov:wasDerivedFrom` in export |
| `language` | record, optional | `dcterms:language` |
| `ethics_ref` | record, optional (placement still open — unchanged) | local |
| `notes` | record, optional; manifest entry, optional | local |
| *(new)* `dataset_uuid` | record, required + object label | `dcterms:identifier` (with `identifier` as the readable form) |
| *(new)* `identifier` | record, required | `dcterms:identifier` |
| *(new)* `creator` | record, recommended | `dcterms:creator` — the intellectual responsibility gap the brief flagged. Person or team name; **leave the guidance text as a placeholder for Neil** — whether this is the PI, the strand, or the coding team is a Centre call |
| *(new)* `bytes` | manifest entry, required | local (Frictionless kin) |
| *(new)* `format` | manifest entry, optional | `dcterms:format` (MIME) |
| *(new)* `spatial` | record, optional | `dcterms:spatial` — the RS3 gap. Free-text place name or `[W, S, E, N]` bbox; keep loose until RS3's first real deposit forces the question |

---

## 4. Prompting changes

Smaller than the data model change — the batch questions were already the dataset questions. The genuine improvement:

**Depositing to an existing dataset loads its record as defaults.** After strand → project → sensitivity → state selection (r4 picker unchanged), if `dataset.meta.json` exists at the prefix:

```
Existing dataset found: csac 3-0 — 40 files, modified 2026-07-29.
Adding to it. Current values will be kept unless you change them.

  Version [3-0]:
  Update the abstract? [y/N]
  Coverage dates for the new files (Enter = within 1989..2025-12-31):
```

Repeat deposits stop re-asking everything — the record answers instead. First deposit to a prefix prompts in full, as now. A record that exists but fails to parse is an error to surface, not silently overwrite.

New prompts otherwise: `creator` (recommended tier, skippable), per the disposition table.

---

## 5. Code touch-points

| Module | Change |
|---|---|
| `sidecar.py` → **rename `record.py`** | Nothing imports it yet — the gateway doesn't exist — so the rename is free now and never again. Field definitions per §3; record assembly; envelope computation and containment validation; manifest union logic; UUID minting. Still no user I/O. |
| `keys.py` | Record key = prefix + `dataset.meta.json`; reserved-name refusal; otherwise unchanged (r3/r4 logic stands). |
| `deposit.py` | Load-existing-record flow (§4); write order (§1 Q4); label flags on upload (§6); verification against manifest; report wording ("dataset csac 3-0: 40 files, 3 added"). |
| `vocab.json` / fetch | Unchanged. |
| *(new)* `export_dcat.py` | §9. Small, separate, optional at runtime. |
| *(new)* `migrate_v03.py` | §8. Not shipped to researchers. |

---

## 6. Transfer details

- Labels at upload: `rclone copyto ... --header-upload "x-amz-meta-dataset-uuid: ..."` (one flag per label). **Verify in tests that the installed rclone passes upload headers through to Ceph** — read back with `rclone lsjson --metadata` on one object. If a floor version is needed, record it in the preflight check rather than assuming.
- Write order enforced in code, not convention: members (with labels) → verify members → record last → re-read record → verify manifest against listing (size match).
- Record rewrite on metadata-only edits (abstract update, version bump with no new files) touches one object; members are never re-uploaded unless their checksum differs from the manifest entry.

---

## 7. JSON Schema and the validation split

One schema, `dataset.schema.json` (draft-07), shipped in the repo:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CRSW dataset record",
  "type": "object",
  "required": ["schema_version", "dataset_uuid", "identifier", "strand",
               "domain", "project", "state", "sensitivity",
               "coverage_start", "coverage_end", "version", "abstract",
               "subjects", "created", "modified", "files"],
  "properties": {
    "schema_version": {"const": "0.4"},
    "dataset_uuid": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
    "identifier": {"type": "string", "pattern": "^rs[1-4]/[a-z0-9-]+/(green|amber)/(0_raw|1_interim|2_final)$"},
    "strand": {"enum": ["rs1", "rs2", "rs3", "rs4"]},
    "domain": {"type": "string"},
    "project": {"type": "string", "pattern": "^[a-z0-9-]+$"},
    "state": {"enum": ["0_raw", "1_interim", "2_final"]},
    "sensitivity": {"enum": ["green", "amber"]},
    "coverage_start": {"type": "string"},
    "coverage_end": {"type": "string"},
    "version": {"type": "string", "pattern": "^[0-9]+-[0-9]+$"},
    "abstract": {"type": "string", "minLength": 50},
    "subjects": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "vocabulary_version": {"type": "string"},
    "creator": {"type": "string"},
    "source_type": {"enum": ["archive", "survey", "scrape", "instrument", "partner", "derived", "other"]},
    "source_detail": {"type": "string"},
    "licence": {"type": "string"},
    "steward": {"type": "string"},
    "depositors": {"type": "array", "items": {"type": "string"}},
    "created": {"type": "string"},
    "modified": {"type": "string"},
    "derived_from": {"type": "string"},
    "language": {"type": "array", "items": {"type": "string"}},
    "ethics_ref": {"type": "string"},
    "spatial": {},
    "notes": {"type": "string"},
    "files": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "required": ["path", "checksum_sha256", "bytes"],
        "properties": {
          "path": {"type": "string", "not": {"const": "dataset.meta.json"}},
          "checksum_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
          "bytes": {"type": "integer", "minimum": 0},
          "coverage_start": {"type": "string"},
          "coverage_end": {"type": "string"},
          "format": {"type": "string"},
          "derived_from": {"type": "string"},
          "notes": {"type": "string"}
        }
      }
    }
  }
}
```

The split to hold in mind: **the shipped tool cannot use `jsonschema`** (stdlib constraint), so runtime validation stays hand-rolled in `record.py` as now. The schema is the contract artefact — used by repo CI and the future gateway. Keep them in step mechanically: a **dev-dependency test** (`pip install jsonschema` in the test environment only, never shipped) validates the tool's emitted record against the schema. Domain and subjects are strings in the schema deliberately — their closed lists live in the runtime-fetched vocabulary, which a static schema can't chase.

---

## 8. Migration of v0.3 deposits

Test material was purged at r3; real v0.3-style deposits are few. A one-off `migrate_v03.py`, run by Neil, not shipped:

1. List prefixes containing `*.meta.json` sidecars.
2. Per prefix: read the sidecars (shared fields are identical across them by construction — verify, warn if not), synthesise the record, build `files` from the listing plus sidecar checksums, mint the UUID, set labels on members, write the record, delete the old sidecars.
3. `--dry-run` first, per the house style.

If the count of real deposits is zero when this lands, delete the script unused rather than maintaining it.

---

## 9. DCAT converter (proves the export claim)

`export_dcat.py`: reads a `dataset.meta.json`, emits a DCAT dataset description (JSON-LD) using the §3 mapping. Stdlib, ~100 lines, no runtime role in deposit. Its existence turns "catalogue ingest is an export job" from an assertion into a demonstration, and it becomes the test that the mapping table is complete — any field the converter can't place is a mapping gap. RO-Crate generation is the same shape and can wait until the archival conversation is live.

---

## 10. Tests (extends 07 §9 and r2–r4)

| Case | Expected |
|---|---|
| First deposit, 3 files | Members land with labels; record last; manifest matches listing |
| Add 2 files to existing dataset | Union manifest (5 entries); version unchanged by default; `modified` updated; `depositors` appended |
| Interrupted after member 2 of 5 (Ctrl-C) | No record write; re-run completes members and writes record; final manifest = 5 |
| Re-run of a completed deposit | No member re-uploads (checksums match); record rewritten; report says "0 added" |
| Metadata-only edit (version bump) | One object touched (the record) |
| Single-file dataset | Valid; envelope = the file's coverage |
| Per-file coverage on some members | Envelope computed, not prompted; containment enforced |
| Member named `dataset.meta.json` | Refused at `keys.py` |
| Existing record fails to parse | Error surfaced; nothing overwritten |
| Label round-trip | `lsjson --metadata` shows all four labels post-upload |
| Emitted record vs schema | Passes `jsonschema` validation (dev-dep test) |
| `export_dcat.py` on the worked example | Every record field either mapped or explicitly local |

---

## 11. Documentation debt

The docx suite is **not** revised in this pass — one batched revision after the build survives contact with the pilot, per the r2 §7 discipline. Seeded list (from the brief, extended):

| Document | Change |
|---|---|
| `06` Sidecar spec → **v0.4** | The major rewrite: retitle around dataset record + manifest; §3 disposition; §5 rules act on datasets; worked example; DC mapping; open questions refreshed |
| `02` Handbook → **v0.6** | §4.1 unit of description; §4.4 dataset versioning; §8.2 write order + manifest; §8.3 reframed; appendix. Plus known stale fragments (§8.3 example's `schema_version: "1.0"` and `data_date`) |
| `03` Recap → **v1.4** | §7–8 examples; fix "Only major versions appear in filenames" (contradicts v0.5); decision table gains dataset-as-unit + manifest rows |
| `00` Register → **v1.5** | Register the above; decide whether r-series specs are listed (suggest: yes, one row, "tool-repo artefacts, not policy") |
| `07` Handoff | Superseded in part by this spec; register note, no reissue |
| `04` Gap analysis | Check G3/G5 wording reads correctly against dataset-level records; likely untouched |

---

## 12. Blocked on / open

- [ ] **Partner endorsement of dataset-as-unit** (sought 29 July). Build proceeds; researcher rollout waits.
- [ ] `creator` guidance text — Centre call on what the field means here. Placeholder shipped.
- [ ] Member *removal* semantics — deliberate curation, out of scope for r5, manual meanwhile.
- [ ] rclone header-upload support against Ceph confirmed by the §6 test.
- [ ] Unchanged from earlier: subjects vocabulary steward review; `ethics_ref` placement; restricted-project (deny) flow from the permissions discussion — none altered by this revision.

## 13. Build order

1. `record.py` (rename + new assembly, envelope, union logic) with unit tests — no network needed
2. `keys.py` reserved-name check
3. `dataset.schema.json` + dev-dep validation test
4. `deposit.py`: write order, verification-against-manifest
5. Load-existing-record defaults flow
6. Object labels + the rclone header round-trip test
7. `migrate_v03.py` (or its deletion, if nothing to migrate)
8. `export_dcat.py`
9. Error-message pass, as ever, last and not optional
