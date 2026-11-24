<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: dataset record section moved out of the README unchanged, so the README can be short -->


# The dataset record

Moved from the README on 24 November 2026, text unchanged.

Every dataset prefix holds one `dataset.<name>.json`: the description
and the files manifest in a single document, written last so its
presence marks a complete deposit. The current schema is **0.6**
(`dataset.schema.json`). Records written as 0.5 are still read; the
tool upgrades them on the way in and writes them back as 0.6, so nobody
converts a record by hand. Beyond the descriptive fields and the
manifest, a record may carry:

- `derived_from` — a list of references to what this dataset was made
  from: another Centre dataset (by its five-part identifier and, once
  known, its UUID) or something outside the store (a URL or a citation).
  The CLI interview, the web form's Origin section and `--provenance`
  all fill it through one rule; the promoter fills in the UUID.
- `provenance` — a list of the activities that produced it, oldest
  first: the kind of step (convert, clean, harmonise, …), the tool with
  its repository and commit, inputs, outputs, who ran it and when. The
  tool writes only what it is told; it never infers provenance.
- `category_history` — what has changed in the dataset's subject terms
  or domain since deposit, and by whom (a steward, or the vocabulary
  itself when a term was renamed, merged, split or retired).

`export_dcat.py` renders a record as a DCAT dataset description with the
derivation and activities expressed in W3C PROV terms, using
`crsw-dc-mapping.json` as the single source of every term. The
depositors' KCL usernames are left out unless `--include-depositors` is
given, so a description sent to a catalogue outside KCL does not carry them.

## The schema and the Dublin Core mapping

`dataset.schema.json` is the contract for the record format, used by CI
and the future ingestion gateway; the shipped tool validates by hand
(standard library only). Tests that need the `jsonschema` package skip
cleanly when it is not installed. The Dublin Core mapping itself ships as
`crsw-dc-mapping.json` — the single source of truth, not a table in a
document — and `export_dcat.py` renders it into a DCAT (JSON-LD) dataset
description, doubling as the proof that every schema field is mapped or
explicitly marked local.
