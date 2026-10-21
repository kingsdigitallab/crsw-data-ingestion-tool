# CRSW Deposit Tool — revision spec (r11): fields set in place, and the record at 0.7

Status: **proposal, not agreed.** Decided with Neil on 21 October 2026
and built in three steps (§6). Read alongside r9 (categories after
deposit, whose change file this extends) and r8 §5 (the read-and-upgrade
path that 0.7 reuses). Nothing here changes the unit of description,
the key layout, the object labels or the members-first-record-last rule.

**Origin:** `docs/sima-case-study-deposit-handoff.md`. Depositing two
third-party exports (an ACLED regional aggregate and a Traffik Analysis
Hub extract) on a researcher's behalf showed four gaps. The record has
no controlled way to say *who provides* a dataset, so a provider's name
and rights go into free text that search cannot group. `spatial` is a
loose, untyped field nobody collects, so coverage in space is invisible
to the find page. A record's licence, notes or creator cannot be
corrected once promoted: `promoter recategorise` touches `subject` and
`domain` and nothing else, and the only alternative is a re-deposit
through the form with no files, which the form is not for. And a
dataset has one `domain`, though a steward may want a second named.

## 1. Fields set in place

### 1.1 The question

How does a steward correct a descriptive field on a record that is
already in place?

- A. Extend the r9 change file with a `set` object: the dataset, the
  fields and their new values, a reason. Applied by `recategorise`
  exactly as `add`/`remove`/`set_domain` are: dry-run first, every
  rewrite a history entry in the record and a line on the audit trail.
- B. A re-deposit through the form or CLI with no files, which would
  rewrite every descriptive field at once and append a depositor.
- C. Hand-edit the record in the bucket.

**Decided: A.** It is small, stays inside the promoter (r10: the store
changes only through the promoter), and the record's own
`category_history` still tells the whole story. C is forbidden by r10
and B is a deposit, not a correction.

### 1.2 The change file

An entry may carry any of `add`, `remove`, `set_domain`, `set` and
`reason`; `set` alone is enough. A `null` value clears the field.

```json
[
  {"dataset": "rs1/legal-sources/green/0_raw/international-instruments-sample",
   "set": {"license": "Third-party material under the originating bodies' terms; not for redistribution",
           "notes": "ICC Legal Tools, UN ODS and OHCHR documents",
           "language": ["en", "fr"],
           "ethics_ref": null},
   "reason": "licence corrected after review"}
]
```

### 1.3 What `set` may write

Only descriptive fields, listed in `record.SETTABLE_FIELDS` in record
order with their shape: `creator`, `source_type` (the enum, now
`record.SOURCE_TYPES`), `source_detail`, `license`, `steward`,
`language` (a list), `ethics_ref`, `notes`; from 0.7 also `provider`,
`spatial` and `spatial_note` (§2). Text must be non-empty; a list must
be a non-empty list of non-empty strings.

What it never writes, and why:

- `subject` and `domain`: they have their own keys (`add`, `remove`,
  `set_domain`) and their own history kinds.
- `derived_from`, `provenance`, `depositors`, `category_history`: these
  accumulate through deposit and promotion and are never overwritten.
- Every required field: identity (`identifier`, `dataset_uuid`, the five
  path parts), `version`, `abstract`, `temporal`, `created`, `modified`
  and the `files` manifest change only through a new deposit, or not at
  all. A wrong abstract is re-deposited; a wrong location is removed
  (r10) and deposited again.

`set_domain` does not touch `steward`: a steward who wants both names
both in one entry (`"set_domain": "geo", "set": {"steward": "..."}`).

### 1.4 The history entry

One entry per field, kind `field_set`, with a `field` key naming it. A
first-time set has no `from`; a clear has no `to`; at least one is
required. List fields carry the lists themselves.

```json
{"when": "2026-10-21T10:00:00Z", "by": "k1078591", "kind": "field_set",
 "field": "license", "from": ["CC-BY-4.0"],
 "to": ["Third-party material under the originating bodies' terms; not for redistribution"],
 "reason": "licence corrected after review", "vocabulary_version": "2026-07-23"}
```

`field` is the one addition to the r9 entry shape, required for
`field_set` and refused on any other kind. The schema carries both rules
(`history_entry.kind` and an `if/then` on `field`); adding a kind and an
optional key is additive, so this part ships inside 0.6 (r9 §1.9).

### 1.5 Rules

- Entries come out in record order whatever order the file used, after
  any `removed`, `added` and `domain_changed` entries from the same
  change: `removed, added, domain_changed, field_set…`.
- A value the record already has, or clearing a field it does not have,
  produces no entry, so a second run with the same file writes nothing
  (r9 §1.7).
- A bad file (unknown field, a required field, wrong shape, an unknown
  `source_type`) is refused before any record is read, exit 2. A change
  that would leave the record invalid, or names no dataset, is refused
  for that dataset with nothing written, exit 1.
- `modified` moves; `vocabulary_version` is stamped; UUID, `created`,
  the files and the key never change; the record's object labels are
  refreshed as for any rewrite.

## 2. The record at 0.7

(Built in step B; written here so the design is in one place.)

### 2.1 `domain` becomes a list, primary first

`"domain": ["quant"]` or `["quant", "geo"]`: a non-empty list of domain
codes, no repeats. The first entry is the primary and sets `steward` at
deposit, as the single code did. Domain is not in the key, so nothing
moves. `set_domain` accepts a code (wrapped) or a list; `domain_changed`
entries carry lists.

### 2.2 `provider`, recommended

A list of slugs from a `providers` section of the vocabulary (§3), e.g.
`["acled"]`; a record of international instruments may name several.
Validation: with a providers list in hand an unknown slug is an error,
as an unknown subject is; without one (an old cache, the vocabulary
repo not yet updated) the shape is checked and one warning says the
slug could not be checked. `source_detail` shrinks to the per-dataset
specifics: which file, which filter, when downloaded.

### 2.3 `spatial` as coded places; `spatial_note`

`spatial` becomes a list of scheme-prefixed codes from the UN M49
geoscheme: `m49:NNN` for the world, regions, subregions and
intermediate regions, `iso:XX` (ISO 3166 alpha-2) for countries, e.g.
`["m49:419"]` or `["iso:TH", "iso:AU"]`. Labels and the hierarchy come
from `places.json` (§4). `spatial_note`, optional free text, carries
what the hierarchy cannot say (sub-national areas, corpora named by
place) and is the field that later hands off to Data Spine identifiers.
Validation as for `provider`: an unknown code is an error when places
are in hand, a warning when not; the shape is always checked.

### 2.4 `creator` by state

Guidance, not a rule: at `0_raw`, the originating organisation
(prefilled from the provider entry where there is one); at `1_interim`
and `2_final`, the Centre researcher or team, with provenance back to
the originator through `derived_from`. The depositor field already says
who did the depositing.

### 2.5 Reading older records

`upgrade_record` runs 0.5→0.6→0.7 in one call, so every tool reads any
record and writes 0.7 (r8 §5). 0.6→0.7: a string `domain` becomes a
one-element list; a string `spatial` moves to `spatial_note` and the
key is dropped; a list of valid codes is kept; any other list is joined
into `spatial_note`. Records in place are rewritten only when something
else touches them, never for the version alone (r9 §1.7).

## 3. Providers in the vocabulary

A `providers` section in `vocab.json`, after `activities`, one entry per
line as for domains:

```json
{"slug": "acled", "name": "Armed Conflict Location & Event Data (ACLED)",
 "url": "https://acleddata.com/",
 "rights": "ACLED terms of use; attribution required; no redistribution beyond the licensee"}
```

`slug` and `name` required; `url`, `rights` (a default licence
identifier or rights statement) and `notes` optional. The validator
checks it; `crsw-vocab` carries it through a rewrite. Starter entries:
`acled`, `tah`, `unodc-sherloc`, `icc-legal-tools`, `un-ods`,
`ohchr-juris`. The form offers the list and prefills Licence from
`rights` and, at `0_raw`, Creator from `name`; a provider not yet listed
is written into `notes` with a warning, which is the prompt to add it.

## 4. Places

`places.json`, generated in the vocabulary repo from the UNSD M49
overview CSV, never edited by hand; a bundled copy ships in the package
and reaches the tool by the vocabulary's own chain (fetch → cache →
bundled). `broader` is the parent, the authority file's convention:

```json
{"places_version": "2026-10-21",
 "source": "UNSD M49 overview CSV, https://unstats.un.org/unsd/methodology/m49/overview/",
 "places": [
   {"code": "m49:001", "label": "World", "kind": "world"},
   {"code": "m49:019", "label": "Americas", "kind": "region", "broader": "m49:001"},
   {"code": "m49:419", "label": "Latin America and the Caribbean", "kind": "subregion", "broader": "m49:019"},
   {"code": "m49:005", "label": "South America", "kind": "intermediate", "broader": "m49:419"},
   {"code": "iso:CO", "label": "Colombia", "kind": "country", "broader": "m49:005", "m49": "170"}]}
```

Search expands the hierarchy at query time, as the subject `broader`
expansion does: a query for a country finds a dataset tagged with its
subregion or with the world, and a query for a region finds one tagged
with a country inside it.

**Exceptions policy.** M49 and ISO 3166 follow UN member positions on
Kosovo, Taiwan, Western Sahara and Palestine. The scheme is adopted as
published; a dataset whose coverage needs a place the scheme does not
name, or names differently from the researcher's usage, records the
nearest code in `spatial` and says so in `spatial_note`. The scheme is
not avoided because of these cases.

## 5. What each route does

- **CLI** (`deposit.py`): `--domain` names the primary; the interview
  picks one. No `--provider` or `--spatial` flag: the form and the
  change file are the routes, and `--dry-run` output is unchanged. The
  CLI never loads places; `spatial` on a re-deposit gets the shape
  check and a warning.
- **Form**: the primary domain select plus "also relevant" boxes; a
  provider select with the "not listed" fallback; a place select over
  the whole M49 tree; `spatial_note` and `notes`; creator help by state.
- **Promoter**: `recategorise` with `set` (§1); `run`, `recategorise`
  and `index` validate `provider` and `spatial` against the vocabulary
  and places in hand; the index carries `domain` (the primary),
  `domains`, `provider`, `spatial` and `spatial_note`.
- **Find page**: a Place filter and an Ask filter key `place`, expanded
  through the places hierarchy; the dataset page shows provider (linked
  to its `url`), places by label, the spatial note and the notes.
- **Export**: `crsw:provider` and `crsw:spatialNote` as local terms;
  `dcterms:spatial` carries the codes.

## 6. Build order and checkpoints

Stop at each and wait for review.

- **A. Fields set in place** (*built*): `record.SETTABLE_FIELDS`,
  `field_set_error`, `field_set` entries, `set` in the change file,
  `source_type` enum in the shared package, schema and runbook.
  *Checkpoint: a change file with `set` rewrites a record with
  `field_set` history and fresh labels, a second run writes nothing, a
  bad file is exit 2, `--dry-run` output unchanged.*
- **B. The record at 0.7** (*built*): schema 0.7, `upgrade_record`,
  `domain` list, `provider`, `spatial`/`spatial_note`, `providers` in
  the vocabulary, `places.py` and the bundled `places.json` (278
  places from the UNSD table of 21 October 2026), the minimum in every
  caller to stay green: the CLI's `--domain` names the primary, the
  form posts one or several codes, the promoter loads places and
  checks both lists, the index keeps `domain` as the primary and adds
  `domains`, a re-deposit keeps the optional fields it is not given.
  *Checkpoint: the 0.5 fixtures upgrade two steps and validate; a 0.6
  record in place stays as it is until a change touches it; the index
  writes.*
- **C. Form, index, find page** (*built*): the form's provider select
  (licence and, at `0_raw`, creator prefilled from the entry; "not
  listed" into the notes with a warning), the place select over the
  whole M49 tree grouped by region, place note, notes, "also relevant"
  domains, creator help by state; the index's `provider`, `spatial`
  and `spatial_note` columns; the find page's Place filter and the
  Ask filter's `place` key, both expanded through the hierarchy; the
  dataset page's provider and place rows. *Checkpoint: a record with
  `provider` and `spatial` goes in through the form, validates at
  finalise against the lists in hand, and is found by a query for a
  country inside its region (the test suite's `TestPlaces`); the live
  check is Sima's ACLED deposit.*

## 7. Not done, deliberately

- The vocabulary repo's side: the `providers` block and `places.json`
  with its generator are raised there once the shape is agreed (the
  handoff's item 8); this repo bundles their output and never writes
  to the repo.
- `spatial_note` in the embedding text: every vector would be re-cut
  once; a follow-up when there is enough coverage to matter.
- Resolving `m49:` codes to UNSD IRIs in the DCAT export.
- Parametrising the `authority` hierarchy helpers over places rather
  than mirroring them in `places.py`.
- A web form for stewards to file a change (r9 §1.4 B) still waits for
  a steward to ask.
