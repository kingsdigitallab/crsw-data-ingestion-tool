# Handoff: depositing Sima Farokhnejad's case-study data, and the spec changes it surfaced

Written 21 October 2026, closing a design session. Two jobs for a fresh session: get Sima's two spreadsheets into CRSW shared storage through the web form with sound metadata, and carry the spec and vocabulary changes the exercise surfaced into the docs and tool. The two jobs are separable; the deposit does not wait on the changes.

Everything here is a fact established in the session, a decision Neil has taken (marked **decided**), a proposal awaiting his agreement (marked **proposed**), or an open question. Nothing is a code change request in itself; where a change touches the tool, it goes through a Claude Code session on the tool repo with the relevant spec revision read first.

**Re-read before trusting this document:** the live form at `crsw-deposit.sites.er.kcl.ac.uk`, `vocab.json` in `kingsdigitallab/crsw-vocabulary` (2026-10-15 at time of writing), and `promoter datasets` for the state of the `crsw` bucket.

---

## 1. The material

Two files from Sima Farokhnejad (RS2, Rights Lab, UoN), supporting her use case *Multilayer Spatial Networks of Conflict and Human Trafficking*. On Neil's machine at `C:\Users\neil_\Documents\Slavery_and_war\Sima- case-study\`. Neither file carries author metadata; both were identified from their structure.

| File | Source | What it is | Size, rows | Coverage |
|---|---|---|---|---|
| `Latin-America-the-Caribbean_aggregated_data_up_to-2026-01-31.xlsx` | ACLED curated regional aggregates | Weekly counts of events and fatalities by country, admin1, event type and sub-event type, with ACLED population-exposure estimate and admin1 centroid. One sheet. | 9.2 MB, 167,350 rows | weeks ending 30 Dec 2017 to 31 Jan 2026; 50 countries across Caribbean (28), South America (14), Central America (7), Mexico |
| `ngo_tah_2003-01-01_2003-12-31.xlsx` | Traffik Analysis Hub (TAH) export, data source filter `ngo` | Incident-level trafficking records, one row per location (source or destination) per incident: trafficking type, sector, recruitment, transport and coercion methods, trafficker and survivor demographics, source URL. Sheet named `in`. | 21 KB, 126 rows | incident dates in 2003; locations include Thailand, Australia, Albania, Poland |

Points established about the files:

- The ACLED file is an **aggregated product**, not event-level data. Created 10 Feb 2026 (so downloaded then), last saved 16 Sep 2026.
- The TAH records in this slice are almost all coded from **UNODC SHERLOC** case-law documents (`URL Source Article`); 24 rows have `Info Source = Other` and no URL. `Data Provider` and `Language` are empty throughout.
- The TAH `Coordinates` column holds values like `14101` and `-34151`: an export quirk, not usable coordinates. Say so in the record; do not try to map them.
- The TAH file is one calendar year of one source filter. Whether more slices are coming is not known. **Ask Sima before depositing**, because the answer decides the dataset name (§4).

Rights. ACLED's terms restrict redistribution of exports beyond the licensee's own organisation, and the Centre spans two institutions. TAH access is restricted to authenticated members of an approved-partner platform (Sima's own use case form). Neither file is the Centre's to license. The form's default licence for green (CC-BY-4.0) would be wrong for both; the licence field is free text and should carry an honest rights statement (§4).

## 2. Decisions taken in the session

- **Deposits live under the researcher's project. Decided.** A `centre-shared` (or `centre`) strand-level prefix for third-party reference data was considered and set aside: the strand element is the access scope, a shared prefix would need its own owner and policy row, and splitting a researcher's inputs between two prefixes made her project incoherent. Cross-project reuse is to be handled by a controlled **provider** term in the dataset record plus search (§3), not by location. The option remains open if the Centre ever commissions a full export or buys a licence collectively; nothing deposited now would need to move.
- **Two datasets, not one.** Different originators, licences, coverage and shape; someone will want to cite one without the other.
- **Both go in at `0_raw`.** Unmodified third-party exports. Sima's own work begins at `1_interim` with `derived_from` pointing at both.
- **`cross-strand` is not a strand.** The use case forms describe the research activity as cross-strand; the deposit's strand is the owning strand whose steward and credentials apply. For Sima that is `rs2`.
- **Spatial is to be recorded in a structured way now**, continent-level, extensible to country without a data-model change. Scheme is proposed below, not yet chosen.
- **A providers list** (ACLED, TAH, UNODC, ICC and so on) should exist as a controlled vocabulary rather than free text in `creator`. Neil's suggestion; shape proposed below.

## 3. Proposed spec and vocabulary changes

None of these is agreed yet. Each names the documents it touches: handbook = `docs/02-data-governance-handbook.md`, spec = `docs/06-dataset-metadata-specification.md`, both in `njakeman/crsw-infrastructure`; vocabulary = `vocab.json` in `kingsdigitallab/crsw-vocabulary`; tool = `crsw-data-ingestion-tool`.

### 3.1 Providers list. Proposed.

A `providers` section in `vocab.json`, alongside `domains`, each entry carrying `slug`, `name`, `url`, `rights` (a default licence identifier or rights statement) and an optional `notes`. Starter entries from material already seen: `acled`, `tah` (Traffik Analysis Hub), `unodc-sherloc`, `icc-legal-tools`, `un-ods`, `ohchr-juris`.

In the record: a new recommended field `provider` (string, a providers slug), with `source_detail` shrinking to the per-dataset specifics (which file, which filter, download date). The provider entry holds the stable URL and the default rights statement, which gives the "not ours to license" case a home at last.

Touches: spec §4.2 (new field, `source_detail` guidance), spec §7 (vocabulary section), vocabulary file and `authority.validate_authority`, schema 0.6 → 0.7, form (dropdown with free-text fallback for a provider not yet listed, which is then a prompt to add it).

### 3.2 `spatial` as coded places. Proposed.

Adopt the UN M49 geoscheme: five continents, subregions, countries, each with a numeric code, countries also carrying ISO 3166 alpha-2. It is a published, maintained hierarchy, so continent now and country later come from the same file with nothing to author. Latin America and the Caribbean is already a node (419) and every country in the ACLED file sits inside it, Mexico included.

In the record: `spatial` becomes an array of scheme-prefixed codes, e.g. `["m49:419"]`, `["iso:TH", "iso:AU"]`; labels are resolved from a `places.json` in the vocabulary repo, generated by a script from the UNSD M49 CSV rather than edited by hand. A short free-text `spatial_note` sits alongside for what the hierarchy cannot say (sub-national areas, "Bosnia and Sudan pilot corpora"), and is the field that later hands off to Data Spine identifiers.

Form: multi-select of continents and subregions now; countries later by deepening the dropdown. Search expands the hierarchy at index time so a dataset tagged 419 is found by a query for Colombia, the same mechanism as the subject `broader` expansion.

Caveat to write into the spec: M49 and ISO follow UN member positions on Kosovo, Taiwan, Western Sahara and Palestine. Adopt the scheme and state an exceptions policy; do not avoid the scheme because of them.

Touches: spec §4.3 (`spatial` from loose string to coded array; `spatial_note`), spec §7, vocabulary repo (`places.json` and its generator), schema 0.7, form, index and find page.

### 3.3 `creator` by state. Proposed, closes an open question in spec §9.

At `0_raw`, `creator` is the originating organisation, prefilled from the provider entry where one exists (ACLED is the originator of its dataset; TAH is closer to publisher of records coded by partner NGOs, which the export does not name). At `1_interim` and `2_final`, `creator` is the Centre researcher or team, and provenance back to the originator runs through `derived_from`. The depositor field, already automatic, is Sima's role for raw third-party data. This is the second concrete case after John Trajer's legal PDFs.

Touches: spec §4.2 and §9; form help text.

### 3.4 `domain` as an array, primary first. Proposed, Neil sympathetic.

Domain is not in the key, so the change is cheap, but it carries the steward, so the first entry sets `steward`. Sima's two datasets stay `quant` only; a centroid column does not make a dataset `geo`.

Touches: spec §4.1, schema 0.7 with `upgrade_record` wrapping the existing string into a one-element list, `recategorise` (`set_domain` takes a list), form.

### 3.5 Source type `mixed`. Argued against; open.

For a raw deposit from one provider there is one source type; a mixed raw deposit is usually two datasets. Mixing happens at interim and final, which `source_type: derived` plus `derived_from` already expresses. Note that `derived_from` is **already an array of references** in schema 0.6 (r8 §5; `upgrade_record` converts the 0.5 string), so the one gap identified in the session for this case was already closed. No change proposed unless Neil still wants the value.

### 3.6 Strand vocabulary. No change.

Stays `rs1`–`rs4`. The `centre-shared` idea is recorded in §2 as considered and set aside.

## 4. Proposed records (to be entered in the form; verify every field against the live form first)

Shared values:

| Field | Value | Note |
|---|---|---|
| Strand | `rs2` | Sima's home strand; her form says RS2/RS3/RS4 but the deposit's strand is the owning one |
| Project | **Neil to choose.** Sima's pilot used `msnocaht` (initials of her use case title). If the bucket is being purged, `conflict-trafficking-networks` is readable; if not, use whatever the form offers as existing rather than create a sibling. | |
| Sensitivity | `amber` for both | TAH plainly: restricted platform. ACLED: redistribution terms restrict sharing beyond the licensee; ask Sima what agreement UoN/Rights Lab holds with ACLED. If an institutional licence covers the Centre it can be reclassified green later (new UUID under r5 Q1, so do not rush it). |
| State | `0_raw` | |
| Domain | `quant` | steward Kevin Fahey |
| Version | `1-0` | |
| Source type | `archive` | existing third-party repository, the same reading as John's PDFs |
| Depositor | automatic | Neil, on Sima's behalf; say so in the abstract |

Dataset 1, ACLED:

| Field | Value |
|---|---|
| Dataset | `acled-latam-caribbean-weekly` |
| Coverage | 2017-12-30 to 2026-01-31 |
| Subjects | `armed-conflict`, `prevalence` |
| Creator | ACLED (Armed Conflict Location & Event Data) |
| Source detail | ACLED curated data, Latin America & the Caribbean weekly aggregated regional file, up to 31 Jan 2026, downloaded Feb 2026 |
| Licence | Third-party data under ACLED terms of use; attribution required; not for redistribution outside the Centre |
| Spatial (once the field exists) | `m49:419`; until then put "Latin America and the Caribbean, 50 countries" in `notes` |
| Notes | Aggregated weekly counts by admin1, not event-level. Event types: protests, violence against civilians, battles, riots, strategic developments, explosions/remote violence. |

Abstract (draft, ≥ 50 words): *Weekly aggregated political violence and protest data for Latin America and the Caribbean, exported from ACLED's curated regional data files. Each row gives counts of events and fatalities for one week, country, first-level administrative unit, event type and sub-event type, with an ACLED population-exposure estimate and the admin1 centroid. Covers 50 countries from late December 2017 to 31 January 2026. This is an aggregated product, not the event-level data; it supplies the conflict layer for a multilayer network study of conflict and trafficking flows. Deposited by KDL on behalf of Sima Farokhnejad. Subject to ACLED's terms of use.*

Dataset 2, TAH:

| Field | Value |
|---|---|
| Dataset | `tah-ngo-incidents` if more years will follow as members of the same dataset; `tah-ngo-incidents-2003` if this is a one-off. Ask Sima. |
| Coverage | 2003-01-01 to 2003-12-31 |
| Subjects | `human-trafficking`, `debt-bondage`, `prosecution`, `osint` |
| Creator | Traffik Analysis Hub (records coded by partner NGOs from UNODC SHERLOC) |
| Source detail | Traffik Analysis Hub export, data source `ngo`, incident dates 2003; most records coded from UNODC SHERLOC case-law summaries |
| Licence | Restricted: Traffik Analysis Hub acceptable-use terms; access to the platform limited to approved partners; Centre use only |
| Spatial (once the field exists) | `iso:TH`, `iso:AU`, `iso:AL`, `iso:PL` at least; check the full country list in the file. Until then, "global; origins and destinations across Asia, Europe and Oceania" in `notes` |
| Notes | 126 rows, one per location per incident. `Coordinates` column malformed in export; not usable. 24 rows have no source URL. |

Abstract (draft): *Incident-level human trafficking records exported from the Traffik Analysis Hub, filtered to NGO-sourced records with incident dates in 2003. Each row describes one location (origin or destination) for an incident, with trafficking type, industry sector, recruitment, transport and coercion methods, trafficker and survivor demographics, and the source article URL. Most records in this slice are coded from UNODC SHERLOC case-law summaries rather than contemporaneous reporting. Access to the underlying platform is restricted to approved partners; this extract is for Centre use only. The coordinates field is unreliable in export and should not be mapped. Deposited by KDL on behalf of Sima Farokhnejad.*

Vocabulary gaps to log for the stewards, not to work around: no term for sexual exploitation short of `sexual-slavery` (most TAH rows are prostitution/brothel cases, which Sima may not want tagged as slavery); `osint` is a loose fit for "coded from case law and press". Both belong with the 9 Oct classification exercise's output.

## 5. The existing deposit, and what the changes mean for it

As of 19 October the store holds one dataset: `rs1/legal-sources/green/0_raw/international-instruments-sample`, uuid `66bed065…`, seven members, John Trajer's legal PDFs, record at schema 0.6 with `merged_with_existing: true` (see `docs/web-deposit/existing-datasets.md` in the tool repo). The test datasets were removed through `promoter remove`. Confirm with `promoter datasets` before relying on this.

What each proposed change does to that record:

| Change | Effect on the existing record | What keeps it compliant |
|---|---|---|
| `provider` (recommended) | Absent; a recommended field may be absent, so the record stays valid | Add it in a later pass: `provider` would be several for this dataset (ICC Legal Tools, UN ODS, OHCHR), which is itself a test of whether `provider` should be a list |
| `spatial` coded array | Absent; optional, so valid. The record carries no `spatial` today | Nothing required. If a value is wanted, `m49:001` (World) is honest for international instruments |
| `spatial_note` | Absent; optional | Nothing |
| `creator` by state | John's record already names the originating bodies, which matches the proposed rule | Nothing |
| `domain` to array | **Breaks** validation of every existing record if done as a hard change | Schema bump to 0.7 with `upgrade_record` wrapping the string, exactly as 0.5 → 0.6 did for `derived_from`; old records are read and upgraded, never rewritten until something else touches them |
| Licence correction | John's licence text should already be the honest third-party statement; check | If it still says CC-BY-4.0, it needs correcting |

The last row exposes the real gap. `promoter recategorise` can add or remove subject terms and set `domain`, and nothing else (`CHANGE_KEYS` in `promoter/recategorise.py`). There is no supported way to set `provider`, `spatial`, `license` or `notes` on a record in place; the alternative, a re-deposit through the form with no files, is not what the form is for. **Proposed:** extend the r9 change file with a `set` object for recommended and optional fields (`{"dataset": "...", "set": {"license": "...", "spatial": [...]}, "reason": "..."}`), applied by `recategorise` with a `category_history` entry of kind `field_set`, so the record's own history still tells the whole story. This is small, stays inside the promoter, and is needed before any of §3 can be applied to records already in place. It should be an r11 note, or an r9 addendum, in the tool repo.

Until that exists, Sima's two deposits should go in with every field that the current form offers filled as in §4, and the fields that do not yet exist (`provider`, coded `spatial`) carried in `notes` and `source_detail` so that nothing is lost, to be moved into their proper fields when the `set` path and the schema bump land.

## 6. Checklist

1. Ask Sima: is the TAH file a one-off or the first of several year slices; what ACLED agreement does the Rights Lab hold; is `conflict-trafficking-networks` (or another name) acceptable as the project slug.
2. Confirm bucket state with `promoter datasets`; decide whether the planned purge happens first, since it decides the project name.
3. Write both abstracts before opening the form.
4. Deposit through the web form, both datasets, fields as §4, gaps carried in `notes`. Record friction as a researcher would meet it.
5. Confirm promotion; inspect both records in place; check the find page and the subject filters.
6. Draft the spec changes in §3.1–3.4 against the handbook and spec in `njakeman/crsw-infrastructure` as a branch for review (handbook §4.6, §6.4; spec §4.1–4.3, §7, §9), with history tables updated in the same pass. Mark each as proposed in the text until agreed.
7. In the tool repo, in a Claude Code session: the r9 `set` extension (§5), then schema 0.7 with `upgrade_record`, then the form fields, in that order.
8. Raise the providers list and `places.json` in the vocabulary repo once the shape is agreed; the generator script for places goes there, not in the tool.
9. Log the two vocabulary gaps (§4) with the stewards.
10. Reply to Sima with both identifiers and an invitation to look at them on the find page.

## 7. Where the detail lives

- Project docs: `claude/john-trajer-legal-pdfs-deposit-handoff.md` (the first third-party deposit; same creator and licence questions), `claude/vocabulary-hierarchy-and-scope-notes-handoff.md` (search expansion, which the spatial hierarchy would reuse).
- Use case forms in the project: `SiW_Use_Case_Template_sima.docx`.
- Tool repo `crsw-data-ingestion-tool`: `CLAUDE.md`; `dataset.schema.json` (0.6); `crsw_deposit/record.py` (`upgrade_record`, `build_record`); `promoter/recategorise.py` (`CHANGE_KEYS`); `docs/specs/DEPOSIT_TOOL_SPEC_R8.md` (provenance, `derived_from` as references), `_R9.md` (recategorisation), `_R10.md` (remove); `docs/web-deposit/existing-datasets.md` (the one live dataset and its re-deposit).
- Infrastructure docs repo `njakeman/crsw-infrastructure`: `docs/02-data-governance-handbook.md` §4.6, §6.4, §8; `docs/06-dataset-metadata-specification.md` §4, §7, §9.
- Vocabulary repo `kingsdigitallab/crsw-vocabulary`: `vocab.json` (2026-10-15), `README.md` for the change process.
- UN M49: unstats.un.org/unsd/methodology/m49/ (overview and CSV download).
