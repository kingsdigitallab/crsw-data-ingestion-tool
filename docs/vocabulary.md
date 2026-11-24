<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: vocabulary section moved out of the README unchanged, so the README can be short -->


# Subjects and domains vocabulary

Moved from the README on 24 November 2026, text unchanged.

Subject terms and data domains are validated against the shared vocabulary,
fetched live so newly approved terms are available immediately. If the fetch
fails (offline, VPN restrictions) the tool falls back to a cached copy, then
to the bundled `vocab.json`, and says which one it used. The subject listing
is shown, numbered, before you're asked — answer with numbers, terms, or a
mix. Type `?` and a number or term (`?3`, `?forced-marriage`) to read what
a term covers before choosing it. Another spelling the vocabulary knows
(`child-soldiers` for `child-soldiering`) is accepted and the term put in
its place, with a line saying so. Unknown terms are refused — propose
additions via the vocabulary repository or your domain steward. Choosing a
domain fills in its steward automatically.

The vocabulary file is a **term authority file** (r9): it lists every term
ever approved, current or retired, with the date each came in, what
replaced a retired one, and an append-only list of the changes (add,
rename, merge, split, retire, move) that got it there. A term may sit
under a broader term in the same facet; a file with no such parents is
simply flat. The tool validates a fetched file against every rule before
trusting it and falls through to the cached or bundled copy if it fails.

Categories can change after deposit. A record whose subject terms have
since been renamed, merged, split or retired is brought up to date by the
promoter, which rewrites only the record (never the files, the UUID or
the key) and appends what it did to the record's `category_history`. A
split gives the dataset every successor term and marks the entry for
review; a steward then removes the ones that do not apply. Stewards make
per-dataset changes through the promoter too, and vocabulary changes
through the vocabulary repository's review process; the promoter never
writes to that repository. See `docs/specs/DEPOSIT_TOOL_SPEC_R9.md`.

Every promoter run leaves its log as one object in the bucket under
`audit/promoter/`, so what was promoted, refused or rewritten, by whom
and when, survives the VM. `python -m promoter audit` reads it back
(filtered by dataset, user, date or action) and `python -m promoter
datasets` lists every record in place. After any run that changed a
record the promoter also rewrites an index of every record in place,
as JSON lines and Parquet under `index/`, for search tools to read
straight from the bucket. With the KCL LLM platform configured it also
keeps one vector per dataset beside the index, made from the record's
title, abstract and subject terms (never a file), for meaning-based
search. With `PROMOTER_PASSAGES=1` it also reads the text of deposited
documents (plain text, Markdown, PDF, Word) of the allowed sensitivities,
splits it into passages and embeds those under `index/passages/`. See
the runbook, section 6.

The web service can also show what is in the store: with a read-only key
configured it gains a "Find data" page (search and filters over the
index), a page per dataset (description, what it was derived from and
what was derived from it, how it was made, files), and the record as
stored. Green files download through the service, resumably, without
the VPN; amber is described for everyone and served according to the
Centre's decision. With the KCL LLM platform configured the service
also offers "Search by meaning or content": the datasets whose
descriptions come closest to the words typed, ranked by meaning, each
with its closest sentence marked. Every step, and every sensitivity
whose metadata may be sent, is a switch in the environment
(`docs/web-deposit/finding-and-reuse.md` §5). With `CRSW_PASSAGES=1` it
also keeps a copy of the promoter's passages on the VM and the same
page shows what the deposited documents say, only for files the
reader could download, with the closest sentence of each passage
marked (§7). Runbook, section 7.

The vocabulary lives at
[kingsdigitallab/crsw-vocabulary](https://github.com/kingsdigitallab/crsw-vocabulary),
which is public, so every tool fetches the current file at run time. To
propose a change, open an issue there and pick the form for it (add,
rename, merge, split, retire, move, describe); the form becomes a pull
request for a steward to approve. A term may carry a scope note (what it
covers and excludes, with the source of the definition) and other
spellings; both are description, not identity, so `describe` changes
them in place with no entry in the change list and no new version. The
same edits can be made from a laptop with `crsw-vocab` (installed with
`pip install -e .` from this repository):

```
crsw-vocab --file vocab.json validate
crsw-vocab --file vocab.json merge debt-bondage --into forced-labour --note "issue #12"
crsw-vocab --file vocab.json describe child-soldiering --scope "Recruitment or use of a person under 18 by an armed force or group." --alias child-soldiers
```
