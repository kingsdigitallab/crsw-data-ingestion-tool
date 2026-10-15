# Handoff: hierarchy-aware subject search, and scope notes on vocabulary terms

> **Status, 9 October 2026.** Part 1 built and committed. Departures: the
> find page and the chat filter are offered the terms in use plus their
> ancestors (not the whole vocabulary); a retired term with no successor is
> a fourth outcome, `removed`, with its own notice; the live vocabulary
> was already hierarchical (five contexts under `armed-conflict` since
> 24 September), so the checkpoints ran against live data.
>
> Part 2 built and committed the same day. Departures: an alias must be
> unique across current terms and is never a current term's slug; the
> `add` change entry does not carry `scope` or `aliases` (the term does);
> `describe` leaves `vocabulary_version` alone; the form keeps the slug as
> the checkbox text and puts the note in a `<details>` after it, no new
> script; vectors are keyed to their text (`text_sha`) rather than
> rebuilt by hand, so a note edited later re-embeds only its carriers, and
> the quiet run checks the text too, reading the index in place. The kit
> changes under `docs/vocabulary-repo/` still need a human pull request to
> the live repository, and its apply-issue workflow installs the tool from
> `main`, so `describe` must be pushed before the "Describe a term" form
> works there. The "First content" below remains a proposal.
>
> 14 October: v0.11.0 on both VMs; the kit and the first two scope notes
> (child-soldiering, forced-labour) merged in the vocabulary repo. One
> decision reversed on seeing it live: the find page's dropdown shows the
> whole vocabulary tree, not the in-use terms plus parents, and the count
> line names only narrower terms the results carry (see
> `finding-and-reuse.md` §5, 14 October).
>
> 15 October: "ranking by meaning is off" was not the code: the promoter's
> timer had never been installed on the internal VM. Installed; the first
> run embedded all six datasets and rebuilt every passages file. The
> runbook (§3, §4) now says how to read the promoter's logs afterwards.

Written 9 October 2026 from a design conversation with Neil. Repos: `crsw-data-ingestion-tool` and the public `kingsdigitallab/crsw-vocabulary`. Two related proposals for the Claude Code session, in the order they should land. Stop at the checkpoints and wait for review, as `docs/web-deposit/plan.md` requires. Nothing here touches `crsw_deposit/`'s stdlib-only rule or writes to the vocabulary repo from the tool.

## Why now

The first researcher-supplied classification (John Trajer, RS1, 9 Oct) proposed new terms, some of which would naturally sit *under* existing ones (`servile-marriage` under `forced-marriage`, `transfer-of-child-for-exploitation` possibly under `human-trafficking`), and supplied definitional text for terms that the file has nowhere to keep. Two gaps follow:

1. The authority file supports `broader` parents (`authority.children_of`, `narrower`, `tree`), the deposit form already renders the tree with indentation (`facet_tree` in `app.py`, `depth-N` classes in `index.html`), but **search ignores the hierarchy**. `Catalogue.search()` filters by exact slug (`catalogue.py` ~line 233) and the text haystack only contains the slugs a record carries. A dataset tagged `servile-marriage` would not be found by a search for `forced-marriage`. Until that is fixed, adding any narrower term makes search quietly worse.
2. A term has `slug`, `facet`, `label`, `status`, dates and optional `broader`. **No scope note.** The definitions a steward relies on to adjudicate, the help text the form should show, and the text that would make subject terms meaningful to the embedding model all have nowhere to live.

Part 1 is a precondition for the vocabulary stewards accepting any `add --broader` or `move`. Part 2 is independent but cheap, and the first real term proposals are waiting on it.

---

## Part 1: search follows the hierarchy

### Behaviour

- A subject filter on term T matches records carrying T **or any descendant of T** (`authority.narrower(doc, T)`), within the same facet. Always on; no toggle. The result count line says so when it applied: "forced-marriage (and 2 narrower terms)".
- A search for a **retired** slug resolves through `authority.map_subjects` to its successors and searches those, with a notice: "`child-soldiers` was renamed; showing `child-soldiering`". Records never carry retired terms (the promoter rewrites them, r9), so without this a retired slug returns nothing and looks like an empty corpus.
- The free-text haystack (`_haystack`, ~line 94) is unchanged: it contains the record's own slugs and labels. Hierarchy applies to the structured subject filter and to the chat model's filter choice, not to word matching.
- `filter_for()` in `llm.py`, which asks the chat model to pick a subject from the vocabulary, is given the tree (facet, slug, depth, label) rather than a flat slug list, so it can choose a parent when the question is broad. Whatever it picks then goes through the same expansion.
- The `/datasets.json` API honours the same expansion as the page; one code path.
- The subject `<select>` on `datasets.html` is grouped by facet and indented by depth (same `depth-N` convention as the deposit form), and lists labels with slugs, not slugs alone.

### Where it goes

- `Catalogue` needs the vocabulary. Give it a `vocabulary` callable or the `VocabularyCache` the app already holds (`crsw_web/vocabulary.py`), so it reads the current doc at query time and picks up refreshes. Do not copy the tree into the catalogue; ask for it.
- Expansion itself is one pure function in `crsw_deposit/authority.py`, since both facets of the question (descendants, retired → successors) are authority-file concerns and the CLI may want it later:

  ```python
  def expand_subject(doc, slug) -> Tuple[List[str], str]:
      """The slugs a search for `slug` should match (itself and every
      current descendant), or, for a retired slug, its current successors
      expanded the same way. The second value says what happened:
      "exact", "narrower" or "replaced"."""
  ```

  Stdlib only, no I/O, flat vocabulary returns `([slug], "exact")`.
- `cdisaw-parquet`'s datasets tab reads `index/datasets.parquet` directly and does its own filtering; it is not changed here. Note it in the decision record as a known divergence until it consumes `/datasets.json` instead.

### Tests

- `tests/test_authority.py`: `expand_subject` on a flat doc, on a two-level tree, on a retired slug with one and with two successors, on an unknown slug (returns `([slug], "exact")`, never raises).
- `tests/test_web_catalogue.py`: a record tagged only with a child is returned for a filter on its parent; a filter on a sibling does not return it; a retired slug returns the successors' records and the response carries the notice; `/datasets.json` agrees with the page.
- `tests/test_web_ask.py`: `filter_for` receives the tree; a parent slug chosen by the model expands.
- `tests/test_llm.py`: the prompt built for `filter_for` lists terms with depth.

### Checkpoints

1. `expand_subject` and its tests, before any web change.
2. The find page against a test index containing one parent/child pair (seed the test vocabulary; the live file is still flat), showing the expansion and the count line.
3. Retired-slug redirect.

### Then, in the vocabulary repo

Add one sentence to the README under "Add a term": a term may be placed under a broader one, and search in the deposit service treats a search for the broader term as including it. This is the signal to stewards that narrower terms are now safe to approve.

---

## Part 2: scope notes (and aliases) on terms

### The field

Add an optional `scope` string to a term, in the sense of a SKOS scope note: what the term covers, what it excludes, a pointer to the legal or scholarly definition it follows. Name it `scope`, not `note`, because `note` is already the free-text field on a `changes` entry ("issue #12") and the two must not be confused. Example:

```json
{"slug": "forced-marriage", "facet": "practices", "label": "Forced marriage",
 "status": "current", "since": "2026-07-23",
 "scope": "Marriage or marriage-like union entered without free and full consent, including servile marriage under the 1956 Supplementary Convention art. 1(c) and child marriage where it amounts to the same. Use for conflict and non-conflict settings alike; the context facet says which."}
```

Optionally, in the same pass, `aliases`: a list of other spellings people use (`child-soldiers` for `child-soldiering`). Used only for matching what someone types in the form or the CLI; never stored in a record; never shown as a term.

### Validation (`authority.validate_authority`)

The validator currently ignores unknown keys, so a `scope` key already passes. Make it explicit: `scope`, if present, must be a non-empty string; `aliases`, if present, a list of distinct slugs (same `SLUG_RE`) none of which is a current term's slug. Retired terms may keep their scope; it documents what the term meant.

### Setting and changing it

A scope note is description, not identity: it does not change which records a term applies to, so it does **not** belong in the append-only `changes` list and the promoter never acts on it. Two routes:

- `crsw-vocab add ... --scope "..."` (and `--alias X`, repeatable) sets it at creation. The add change entry stays as it is; the term carries the field.
- `crsw-vocab describe SLUG --scope "..." [--alias X] [--clear-aliases]` edits it on an existing term, current or retired, validates, writes. No change entry. The git history of the vocabulary repo is the audit trail for description edits, which is proportionate.

In the vocabulary repo: the "Add a term" issue form gains "Scope note" (required — a steward should not approve a term nobody has defined) and "Other spellings" (optional); a new "Describe a term" form drives `describe`; `apply_issue.py` maps both. The README gets a short paragraph on what a good scope note contains: a one-sentence definition, what it excludes, the source of the definition where there is one.

### Where it surfaces

- **Deposit form** (`index.html`): each term's label gets `title="{{ scope }}"` for hover, and a small expand control (`<details>` or a button toggling a `.scope` span) so it works on touch and for screen readers; keep the checkbox list compact by default. Aliases are not shown; the facet filter box, if there is one, matches on them.
- **CLI** (`deposit.py`): the subject listing accepts `?slug` to print a term's scope note, and the "unknown subject" message suggests the term an alias points to ("did you mean child-soldiering?"). `resolve_subjects` accepts an alias and substitutes the slug, saying so. `deposit.py` remains the only file that talks to the user.
- **Find pages** (`datasets.html`, `dataset.html`): subject chips carry the scope as hover text; the filter `<select>` shows labels.
- **Chat filter** (`filter_for`): include scope notes in the term list given to the model. This is the single biggest help to its term choice.
- **Embeddings** (`promoter/embed.py`, `text_for`): append the label and scope note of each of the record's subjects, not the bare slug. Coordinate with the fingerprint work: this changes the embedded text, so it must land as a deliberate re-embed, ideally in the same rebuild as the fingerprint change rather than a second one. Guard against scope notes swamping a short abstract: subjects after the abstract, one line each, label then scope.

### Tests

- `tests/test_authority.py`: scope present and valid; scope empty string rejected; aliases valid; alias colliding with a current slug rejected; alias not slug-shaped rejected.
- `tests/test_vocab_cli.py`: `add --scope --alias`; `describe` on current and retired terms; `describe` writes no change entry; `--clear-aliases`.
- `tests/test_web_form.py`: scope rendered as title and in the expand control; terms without scope render unchanged.
- `tests/test_deposit.py`: `?slug` prints the scope; an alias resolves with a message; an unknown term that is nobody's alias is still refused.
- `tests/test_promoter_embed.py`: `text_for` includes label and scope for subjects; a vocabulary without scope falls back to labels; slugs alone never appear in the text.

### Checkpoints

1. Authority validation and CLI, with tests, before any surface change.
2. Deposit form rendering, screenshot.
3. Embedding text change — show `text_for` output for one record before and after, and confirm the rebuild plan with Neil before running it.

---

## First content for the stewards (not for Claude Code to decide)

From the 9 Oct classification exercise. Proposed as vocabulary repo PR #6 on 15 October 2026 (`mass-atrocity`, `slave-trade` with spellings `slave-trading` and `slaving`, and the two scope notes; the `human-trafficking` note is provisional wording and says so); the alias was PR #5.

- `mass-atrocity` (contexts, top level): slavery practices committed in genocide, crimes against humanity, ethnic cleansing or other widespread or systematic attacks on a civilian population, whether or not an armed conflict is under way.
- `slave-trade` (practices, top level): capture, acquisition, transport or sale of persons for enslavement; needed for the pre-twentieth-century material and the SlaveVoyages data already in the corpus.
- Scope notes for `forced-marriage` (to cover servile and child marriage) and `human-trafficking` (whether art. 1(d) of the 1956 Convention falls inside it; a legal specialist to say).
- Alias `child-soldiers` → `child-soldiering`.

Ownership of these decisions is the open question raised with the Centre on 8 Oct; until a steward is named, they stay as proposals.
