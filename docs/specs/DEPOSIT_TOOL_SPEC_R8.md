# CRSW Deposit Tool — revision spec (r8): provenance

Status: **proposal, not agreed.** Written 23 Sept 2026 after the catalogue
survey; nothing here is built. Revised the same day to fold in the
decisions from `DEPOSIT_TOOL_SPEC_R8_review.md` (marked *decided*). Read alongside r5 §4 (dataset-level
`derived_from`) and r6 §3 (Dublin Core alignment), both of which this
extends.

**Supersedes:** r5 §4 and the `derived_from` rows of r5 §6 and r6 §3.1
where they speak. Everything else in r2–r7 stands.
**Origin:** the catalogue evaluation (Sept 2026) concluded that whichever
platform sits on top of the store — InvenioRDM is the current favourite,
Dataverse the fallback — it will read provenance out of the dataset
record and will not infer it. Two gaps in the v0.5 record stop that
working: `derived_from` is one free-text string, and nothing records what
transformed a dataset. The cdisaw-parquet deposit (`records/dataset.cdb90.json`,
17 Sept 2026) shows both: its derivation is a bare GitHub URL and the
fact that James's ingest script was run unchanged against a Parquet
writer survives only in the `notes` prose.
**Dependency:** none new. Partner endorsement of r6 dataset-as-unit is
still outstanding; this revision does not change the unit.

---

## 0. What provenance means here, and what it does not

Three things a catalogue needs to answer for any dataset:

1. **What was it made from?** — other Centre datasets, or things outside
   the store. This is *derivation*.
2. **What made it?** — a script, a notebook, a manual process, at a known
   version, run by someone at some time. This is the *activity*.
3. **Can the activity be run again?** — only if (2) names the code
   precisely enough. This is what makes a pipeline a reusable asset.

Deliberately **not** provenance in this sense:

- *Acquisition* — how raw data was obtained (`source_type`,
  `source_detail`). r5/r6 were careful to map these to
  `dcterms:provenance` and not `dcterms:source`; that distinction stays.
- *The deposit act itself* — who uploaded it, when, and via which route.
  `depositors`, `created`, `modified` and the promoter's JSON log already
  hold this. Recording every deposit as a provenance activity would bury
  the transformations under upload noise.
- *Staging → prefix promotion* by the promoter. A prefix strip, not a
  transformation; the record is byte-identical before and after (Phase 4).

The shape below is W3C PROV with plain names: `derived_from` is
`prov:wasDerivedFrom`, an activity's `inputs` are `prov:used`, its
`outputs` are `prov:wasGeneratedBy`, its `tool` and `agent` are
`prov:wasAssociatedWith`. The mapping file already declares the `prov`
namespace (r5 §3); this revision starts using it. Researchers never see
PROV terms; the export does.

## 1. `derived_from` becomes a list of references

```
was:  "derived_from": "rs2/csac/amber/1_interim"          (any string)
now:  "derived_from": [
        {"kind": "dataset",
         "identifier": "rs2/csac/amber/1_interim/csac",
         "dataset_uuid": "…", "version": "1-0"},
        {"kind": "external",
         "url": "https://github.com/jrnold/CDB90",
         "citation": "Arnold, J. (2014). CDB90 … GitHub."}
      ]
```

Two kinds, closed:

| kind | required | optional | resolves to |
|---|---|---|---|
| `dataset` | `identifier` (five-part, r6 §2) | `dataset_uuid`, `version` | another record in the store |
| `external` | `url` **or** `citation` | the other one, `retrieved` (ISO date) | something outside the store |

`dataset_uuid` is optional on input because the depositor may not know
it, but it is what a catalogue should key on: identifiers move when a
dataset is reclassified (r5 Q1 mints a new UUID on promotion, so the
*old* UUID is exactly the stable handle for "the thing this came from").
Where the CLI can read the parent prefix it fills `dataset_uuid` and
`version` in from the parent's record and says so; the web route's
staging key cannot read destinations (CLAUDE.md, out of scope), so it
leaves them blank and the promoter fills them (§4).

Rules:

- `source_type: derived` with an empty `derived_from` is a **warning**,
  not an error — "derived, from what?" — matching the tone of the
  abstract guidance. The reverse (`derived_from` present, `source_type`
  not `derived`) is fine: a survey dataset can still cite a sampling frame.
- A `dataset` reference whose `identifier` equals the record's own is an
  error.
- The manifest-level `files[].derived_from` (r5 §6, "rare per-file
  lineage") stays a string, and its export is unchanged (*decided*). It
  has no known user and none of the 54 real records sets it. Structuring
  only the dataset level and not the file level would make the exporter
  render one term two ways; do both or neither, and neither is right
  until someone needs it.
- One rule turns typed text into a reference (*decided*):
  `record.reference_from_text`. A five-part identifier becomes a
  `dataset` reference; anything that parses as a URL becomes `external`
  with `url`; anything else `external` with `citation`. The CLI
  interview, the web form and the 0.5 upgrade path all call it.

## 2. `provenance`: the activities that produced this dataset

A new optional record field, a list, one entry per transformation step,
oldest first:

```json
"provenance": [
  {
    "activity": "harmonise",
    "description": "(CD)ISaW ingest script for cdb90 run unchanged against the Parquet shim instead of PostGIS; 18-column crws-nodes projection",
    "tool": {
      "name": "cdisaw-parquet",
      "repo": "https://github.com/kingsdigitallab/cdisaw-parquet",
      "commit": "3f2a9c1e",
      "command": "cdisaw-parquet sweep --source cdb90 && cdisaw-parquet project"
    },
    "inputs":  [{"kind": "external", "url": "https://github.com/jrnold/CDB90"}],
    "outputs": ["wide/events/cdb90.parquet", "crws/events/cdb90.parquet"],
    "agent": "k1078591",
    "started": "2026-09-17T08:40:00Z",
    "ended":   "2026-09-17T08:59:10Z"
  }
]
```

| field | required | notes |
|---|---|---|
| `activity` | yes | short label. Suggested closed list to start: `convert`, `clean`, `harmonise`, `aggregate`, `geocode`, `anonymise`, `merge`, `subset`, `manual`, `other`. Held in `vocab.json` next to domains so it can grow without a release (r2 §2 pattern), with `keys`-style fallback. |
| `description` | no | one or two sentences; where the `notes` prose about processing should now go |
| `tool` | no | object: `name` required; `repo`, `commit`, `version`, `command`, `notebook` optional. `manual` activities usually have none. |
| `inputs` | no | list of references in the §1 shape, **or** bare member paths of this dataset (a step that consumed an earlier step's output) |
| `outputs` | no | member paths of this dataset that this step produced; must exist in `files[]` |
| `agent` | no | KCL username, as `depositors` |
| `started`, `ended` | no | UTC ISO, as `created` |

`tool.repo` + `tool.commit` is the reusable-asset handle. A catalogue can
list every dataset produced by the same repo, and someone in 2031 can
check out that commit. The tool does not verify the commit exists — it
cannot without network access to the repo — but it warns when the value
is not 7–40 hex characters (*decided*: nothing more, since a branch name
cannot be told from a short hash in general).

**Nothing is invented.** The tool writes what the depositor or the
calling program gave it. It never fills `provenance` from the deposit
itself, and never guesses `activity` from filenames.

## 3. Capture: three routes, one implementation

All three feed the same `meta["provenance"]` and `meta["derived_from"]`
into `deposit_logic.assemble_record`; validation is `record.validate_record`
only (hard constraint: one implementation).

**Programmatic** (cdisaw-parquet and its successors) — the main route
for anything with real pipeline provenance. These already import
`record.py` by path; they build the lists directly. The shim/runner
knows the commit (`git rev-parse HEAD`), the argv, the start and end
times and the output files, so the whole block is mechanical. No user I/O.

**CLI** — `--provenance FILE.json` accepts a file holding either the
`provenance` list or an object with `provenance` and `derived_from`.
Validated with the record; errors name the entry index. The interactive
interview adds two questions, both skippable:

- after `source_type`: "What was this derived from? Dataset identifier
  or URL, one per line, blank to finish" → §1 references. Replaces the
  current single "Parent object key" prompt. A five-part identifier
  becomes a `dataset` reference; anything else an `external` one.
- after the abstract: "Was this dataset produced by a script or
  notebook you can name? [y/N]" → on yes, `tool.name`, `repo`, `commit`,
  `activity` from the list, optional description. One activity only in
  the interview; more than one is `--provenance`.

**Merge rule, one place, both routes** (*decided*). The rule lives in
`deposit_logic.assemble_record(meta, existing, …)`, not in either
caller: `provenance` is the existing list followed by whatever `meta`
supplies (activities append; deposits are additive; running the same
pipeline twice records two activities, which is correct); `derived_from`
is `meta`'s list if supplied, else the existing one. The CLI therefore
no longer copies `derived_from` from the existing record itself. For the
web route, where the service cannot read the destination, the promoter's
merge applies the same rule.

**Web form** — an optional "Origin" section: a repeatable
derived-from row (identifier or URL) and the same single-activity
fields. Both hidden behind a disclosure so the common case (a scanned
archive, nothing derived) stays a short form. `crsw_web.metadata` parses
them into the same `meta` keys; no logic of its own.

## 4. Promoter: resolve, don't invent

With the real key the promoter can see destinations, so it does two
things the web route could not:

- For each `dataset` reference lacking `dataset_uuid`, read the parent
  record at `{identifier}/dataset.<dataset>.json` and fill in
  `dataset_uuid` and `version`. Logged as a JSON line
  (`"action": "resolved_reference"`). A parent that does not exist is
  a **warning** in the report, not a block — a researcher may
  legitimately deposit a `2_final` before its `1_interim` sibling — the
  reference is left as given and the deposit still promotes. Reading a
  parent record needs no authorisation: `PROMOTER_AUTHORISED` gates
  depositing to a strand, and citing is not depositing (*decided*).
- Confirm every `provenance[].outputs` path is in the manifest (also a
  `validate_record` error, so this is belt and braces).

**Where the corrected record is written** (*decided*): in staging first.
The promoter holds the real key and can write under `staging/`, so it
rewrites the staged record with the resolved references, then promotes
exactly as today: members first, record last, verify, delete markers.
The Phase 4 invariant that the staged record and the destination record
are the same bytes holds, the promoter's existing verification needs no
exception, and the resolution is still a logged action. If the staged
record was upgraded from 0.5 on read, that is logged too
(`"action": "record_upgraded"`).

## 5. Schema, mapping, export

**`dataset.schema.json` → 0.6.** The type of `derived_from` changes from
string to array, which is not additive, so the version bumps.
`parse_record` accepts `0.5` and `0.6` (*decided*: the schema lists both
versions and allows the legacy string form of `derived_from`, so an
existing record still validates; every tool writes `0.6`). A `0.5`
record is upgraded on read by `record.upgrade_record` (string
`derived_from` → one reference via `reference_from_text`), then written
back as `0.6`; upgrading a `0.6` record is a no-op, which the four
write-then-read-back verifications depend on. Nobody converts a record
by hand; when the promoter or the CLI merges a `0.5` destination record
with a new deposit, the result is `0.6` and the log says so. Anything
newer is still refused (never blind-overwrite the future).
`REQUIRED_FIELDS` unchanged; `provenance` joins `OPTIONAL_FIELDS`.
cdisaw-parquet, which imports the record module and generated the
existing corpus, must be told to write `0.6`.

**`crsw-dc-mapping.json`** gains:

| field | term |
|---|---|
| `derived_from` | `dcterms:source` / `prov:wasDerivedFrom`; a `dataset` reference renders as the parent's `dcterms:identifier` (UUID if present, else identifier), an `external` one as its URL |
| `provenance` | `prov:wasGeneratedBy` → a `prov:Activity` per entry with `prov:used`, `prov:wasAssociatedWith` (`tool` as `prov:SoftwareAgent`, `agent` as `prov:Person`), `prov:startedAtTime`, `prov:endedAtTime` |
| `provenance[].activity` | `crsw:activityKind`, local |

`export_dcat.py` is designed to fail the build on an unmapped field, so
adding `provenance` without a mapping row is caught by
`tests/test_export_dcat.py`. Extend the converter to render the activity
graph; that is the proof that the PROV claim is mechanical, in the same
spirit as r5 §9. An InvenioRDM importer, when it comes, maps a
`dataset` reference to `relatedIdentifiers[IsDerivedFrom]` and the
activity list to a custom field — both one-liners once the record holds
the structure.

## 6. What this does not do

- No pipeline registry. The `tool` object is the asset handle; a list
  of known tools, or a `pipelines/` prefix in the bucket, is a catalogue
  concern and waits for the platform decision.
- No lineage *graph* in the tool. The record holds edges; drawing the
  raw → interim → final picture is the catalogue's job.
- No change to the unit of description, key layout, labels, or the
  members-first-record-last rule.
- No automatic provenance from the promoter, the web service, or file
  inspection. Empty `provenance` is a true statement ("nobody told us"),
  not a failure.

## 7. Build order and checkpoints

Stop at each and wait for review.

1. **Record and schema** — `record.py` (`REFERENCE_KINDS`,
   `validate_reference`, `validate_activity`, `upgrade_record`,
   changes to `build_record`/`validate_record`), `dataset.schema.json`
   0.6, `deposit_logic.assemble_record` carrying the two new keys,
   `vocab.json` activity list, tests in `test_record.py`,
   `test_schema.py`, `test_deposit_logic.py`, **and** `test_promoter.py`,
   `test_web_form.py`, `test_web_deposits.py`, because the promoter and
   the web service parse records through the same version gate. Upgrade
   path tested against real cdisaw-parquet records kept as fixtures.
   *Checkpoint: schema 0.6 agreed; a 0.5 record round-trips; a 0.5
   destination record merges with a 0.6 deposit and is written as 0.6.*
2. **Mapping and export** — `crsw-dc-mapping.json`, `export_dcat.py`,
   `test_export_dcat.py`.
   *Checkpoint: the cdb90 record exports as DCAT + PROV and someone
   who knows PROV has looked at it.*
3. **CLI** — `--provenance`, the two interview questions, carry-over on
   re-deposit, `--dry-run` output extended and kept stable.
   *Checkpoint: Neil deposits with `--provenance` from cdisaw-parquet's
   generated file.*
4. **Web form and promoter** — `crsw_web.metadata`, template, JS;
   promoter resolution (§4) with its logging.
   *Checkpoint: a web deposit with an unresolved reference promotes with
   the UUID filled in, and the staged and destination records are the
   same bytes before the delete markers.*
5. **Docs** — r8 folded into the README's record section, the Dataset
   Metadata Specification in `crsw-infrastructure`, and the researcher
   how-to (one paragraph: "if a script made this, name it").

## 8. Open questions

- **Closed list or free text for `activity`?** The list above is a
  guess; RS3's geospatial steps (reproject, mosaic, resample) will want
  their own terms. Vocabulary-managed seems right, but the steward review
  that r5 asked for is still outstanding.
- ~~Should the promoter rewrite records (§4)?~~ Decided: yes, in
  staging, before promotion (§4).
- ~~`files[].derived_from`~~ Decided: stays a string (§1).
- **Provenance across the `1_interim` → `2_final` boundary.** Today that
  is a fresh deposit with a new UUID and a `derived_from` pointing back.
  A `promote-state` command that copies members and writes the reference
  automatically would make the raw → interim → final chain reliable
  rather than depending on the depositor remembering. Out of scope here;
  probably the next revision.
- **Version pinning of the reference.** `version` on a `dataset`
  reference records which version of the parent was used; the parent
  may since have been re-deposited. Whether the catalogue treats that as
  a warning is its decision, but the record should carry the value.
