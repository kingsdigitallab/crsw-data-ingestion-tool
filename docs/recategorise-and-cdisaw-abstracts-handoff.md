# Handoff: vocabulary changes should reach records without a person; the cdisaw abstracts need cleaning

5 November 2026. For a Claude Code session on `crsw-data-ingestion-tool` (items A and C) and `cdisaw-parquet` (item B). Read `CLAUDE.md` in each, then `docs/specs/DEPOSIT_TOOL_SPEC_R9.md` (recategorisation) and `R11.md` §1 (fields set in place). Three items, in build order. Nothing here touches the deposit path, promotion's copy logic, the key layout or the sensitivity switches.

**Where this came from.** At the Centre's infrastructure meeting on 4 November Neil described vocabulary changes as reaching every record automatically. They do not: `promoter run` (on the five-minute timer) promotes and rebuilds the index; replaying the vocabulary's change list over records already in place is `promoter recategorise`, which a person runs. Separately, the first live look at a cdisaw record (`centre/cdisaw/green/2_final/gdelt-events-v1`) showed an abstract built for a September test deposit: it ends with a technical tail ("Harmonised copy produced by cdisaw-parquet… Dataset-level domain: context…") and, for GDELT 1.0 only, a maintainer's stub description ("Absent from cdisaw datasets.yaml", a script name). The record's `domain` field is correct (`quant`); the word "context" is text inside the abstract. And `abstract` is not in `record.SETTABLE_FIELDS`, so the review pass the surfacing plan promised (decision 9: Centre reviews generated subjects and abstracts, applied through recategorise) cannot correct an abstract in place.

---

## A. Vocabulary changes replay on the timer (tool repo)

**Decision (Neil, 5 Nov):** the vocabulary's own `changes` list is replayed over records automatically after each promoter run, when the vocabulary version has moved. The steward's per-dataset change file (`--changes FILE`: add, remove, set_domain, set) stays a command a person runs with a dry run first. The distinction: vocabulary changes have already been reviewed through the repository's issue forms; a change file is a judgement being made now.

Shape:

1. In `cmd_run`, after promotion and before the index rebuild: compare the fetched vocabulary's `vocabulary_version` with the version last applied. Hold that marker in the bucket, not on disk (the promoter is a container): a small object under `PROMOTER_INDEX_PREFIX`, say `index/vocabulary-applied.json` with `{"vocabulary_version": …, "applied": <UTC>, "run_id": …}`. Absent marker = never applied = replay.
2. If it has moved, call `recat.plan(client, bucket, vocab_dict, changes=[], by="vocabulary", …)` and `apply`, exactly as `cmd_recategorise` does with no change file, then write the marker. Log `vocabulary_replayed` with the two versions and the count of records rewritten; `vocabulary_current` when nothing moved (quiet runs should stay quiet in the journal, so make that a debug-level or summary-only line).
3. Rebuild the index, embeddings and passages once, after both promotion and replay, not twice.
4. A failed replay must not block promotion or the index: log `vocabulary_replay_failed` with the reason and carry on; the marker is not written, so the next run tries again.
5. `promoter recategorise` with no `--changes` still works by hand and still writes the marker, so a manual replay and the timer agree.
6. Dry run: `promoter run --dry-run` reports what the replay would do without writing anything, as it does for promotion.

Tests (`tests/test_promoter.py`): marker absent → replay runs; marker equal → no replay; marker older and a rename in `changes` → the affected record is rewritten with a `category_history` entry whose `by` is `vocabulary`; replay failure → promotion and index still complete, marker unchanged.

Docs: `docs/web-deposit/runbook.md` (the timer now applies vocabulary changes; how to see it in `promoter audit --action vocabulary_replayed`); `R9.md` §1.7 gets a one-line addendum; `CLAUDE.md` promoter paragraph. In `crsw-infrastructure`, the handbook §4.6 sentence "the promoter can bring every dataset record in the store up to the current terms" becomes "does, on its next run"; Neil will make that edit.

## B. The cdisaw record generator: abstracts for researchers, provenance in its own field (cdisaw-parquet)

`src/cdisaw_parquet/deposit.py`, `_abstract()`:

1. The abstract is the registry title and description, full stop. Where that is under 50 words, pad with what a researcher would want to know rather than how the file was made: coverage ("Covers {n} countries, {start} to {end}"), what one row is (`record_kind`: "one row per event" / "one row per country-year indicator"), and the row count. Never the script name, the file layout, or "Dataset-level domain".
2. The technical tail moves to where the record already has places for it: the harmonisation step is a `provenance` activity (`activity: harmonise`, `tool: {name: cdisaw-parquet, repo, commit}`, `description: "(CD)ISaW ingest script run unchanged against the Parquet shim"`), which r8 built for exactly this; the layout note ("wide/ 35 columns, crws/ 18 columns") goes in `notes`; the explorer sentence is already a link on the dataset page since r12 and need not be in the abstract.
3. `registry.py` line ~29–36: the GDELT 1.0 stub description is rewritten for a reader ("GDELT 1.0 event archive, 1979–2013 (this copy to 1990): machine-coded political events from news, geolocated to the action or the actor's country. Not part of (CD)ISaW's own dataset list; harmonised by cdisaw-parquet from the same ingest script.") and the "absent from datasets.yaml" fact becomes a `notes` line.
4. Regenerate the 54 records (`cdisaw-parquet deposit` to the staging directory only); diff one against its current form to check nothing else moved. Subjects, provider, spatial, uuid and manifest must be unchanged.

## C. Make the review pass possible: `abstract` settable in place (tool repo)

1. Add `("abstract", "text")` to `record.SETTABLE_FIELDS`. Keep the 50-word guidance as a warning in the plan output, not a refusal (the same posture as deposit).
2. Add `("subject", …)`? No: subjects already have `add` and `remove` in the change file, with hierarchy awareness. Leave them.
3. `recategorise --changes` then applies the 54 regenerated abstracts in place: a change file of 54 entries `{"dataset": "centre/cdisaw/green/2_final/<slug>", "set": {"abstract": "...", "notes": "..."}, "reason": "generator corrected; abstract rewritten for readers"}`, produced by a small script in cdisaw-parquet from the regenerated records (so the two repos still never write into each other's store). Dry run, read the plan, apply. Each record gains a `field_set` history entry; the embeddings are re-cut by the index rebuild that follows.
4. The same mechanism is what decision 9 (Centre review of subjects and abstracts) uses when the stewards get to it; this handoff does not do that review, only makes it possible and fixes the obviously wrong text.

## Not in this handoff

- Ranking specific datasets above global ones on Find data (a dataset tagged `m49:001` matches every place; GDELT tagged `sexual-slavery` at dataset level swamps the subject filter). Needs the per-dataset "covers" summary from the spine work, or a simpler heuristic (number of place codes, number of subjects); a separate item.
- The four unmapped typology codes (`child_slavery`, `chattel_slavery`, `state_imposed_labour`, `serfdom`): stewards' decision, raised when the vocabulary group convenes.
- The explorer's datasets tab not following hierarchy expansion: known divergence, separate item.

## Order and checks

A first (it is the one that was misdescribed to the Centre), then C (small), then B and the change file. Before finishing: the GDELT page on the live service reads like a description of data, not of a pipeline; `promoter audit --action vocabulary_replayed` shows a replay after a vocabulary bump on the test bucket; `python -m unittest` and `python -m pytest` pass in the tool repo; `pytest` passes in cdisaw-parquet with the answer-key unchanged.
