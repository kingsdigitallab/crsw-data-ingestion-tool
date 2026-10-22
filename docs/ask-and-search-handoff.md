# Handoff: retire "Ask in your own words"; one page for search by meaning or content

Written 22 October 2026 after the first meaning-search test against Sima Farokhnejad's two amber deposits. For a Claude Code session on `crsw-data-ingestion-tool`. Read `CLAUDE.md`, `docs/web-deposit/finding-and-reuse.md` §5 and §7, and `docs/web-deposit/weak-band-handoff.md` first. Nothing here touches the deposit path, the record, the promoter's writes or the sensitivity switches.

**What was observed.** On the Find data page, *Court cases about women moved abroad and forced to work in brothels* was "Understood as: strand rs3, sensitivity amber, subject human-trafficking, words: trafficking women brothels." Nothing matched all of that, and the fallback showed the one dataset with a vector. Two things were wrong at once: the strand legend in `FILTER_PROMPT` describes a different centre (rs3 is geospatial, not law and policy; see the handbook appendix), and the model inferred a strand and a sensitivity from a question that implied neither, then applied them as a hard filter. Separately, the two datasets that should have ranked first had no vectors because `PROMOTER_LLM_SENSITIVITIES` was `green`; Neil has set it to `green,amber` and re-run `promoter index --embed`. That part is configuration.

**Decision (Neil, 22 Oct 2026).** The question-to-filter step is not helping and is retired. Find data becomes filters plus a words box; everything by meaning moves to one page, renamed **Search by meaning or content**, which ranks dataset descriptions and passages together.

---

## 1. Find data (`/datasets`)

- Remove the "Ask in your own words" form, the `ask` query parameter, the "Understood as" line, the "Nothing matches all of that" notice, and `ask_enabled` from the template context.
- Keep the Search words box and the filter dropdowns (strand, state, sensitivity, subject, place), with the hierarchy expansion for subject and place as now. The words box matches titles and abstracts, as `Catalogue.search(q=...)` does today.
- Add one line under the heading: "To search by what a dataset is about or what its documents say, use *Search by meaning or content*." with the link.
- `/datasets.json` loses `ask`.

## 2. Search by meaning or content (`/search`)

Rename in the nav, the `<title>` and the `<h1>`. Intro becomes: "What the deposited datasets are about, and what their documents say." Same query box and ordering radios as now.

Results in two groups:

- **Datasets.** `Catalogue.rank` over the question's vector (the embedding of dataset name, abstract, subject labels and scope notes, source detail), the same reranker over the top few that `ask` used, each row showing title, strand and sensitivity badge, an abstract excerpt with the closest sentence marked as the passages do, provider and places. Weak band as for passages: the same `CRSW_SEARCH_WEAK_*` settings unless a separate pair proves necessary on a real corpus. Datasets without a vector are not listed here (they are on Find data by words); say so in the help text.
- **Passages.** As now.

Under "Exact matching text", datasets match on words in title and abstract; under "By close meaning", by vector. "Why this?" works for a dataset row as it does for a passage (the record's abstract is what the platform explains against; only for sensitivities in `CRSW_LLM_SENSITIVITIES`).

`/search.json` gains a `datasets` list beside `passages`.

## 3. Code to remove or move

- `crsw_web/llm.py`: `FILTER_PROMPT`, `FILTER_KEYS`, `filter_for`, `_subject_lines`/`_place_lines` if nothing else uses them. The chat alias stays for the reranker, "why this?" and OCR.
- `crsw_web/catalogue.py`: `Catalogue.ask` goes; its ranking and reranking part is lifted into a function the search page calls (`rank_datasets(question, platform, sensitivities, limit)` or similar), so the logic is written once. `subject_tree` and `place_tree` stay if the filter dropdowns use them.
- `crsw_web/app.py`: `/datasets` and `/datasets.json` handlers lose the ask branch; `/search`, `/search.json` and `/search/why` gain the datasets group.
- Tests: `tests/test_web_ask.py` is retired; its vector/reranker/sensitivity cases move to `tests/test_web_search.py` as dataset-group cases (with and without vectors; sensitivity scope; weak band; a dataset row in `/search.json`).
- `.env.example`: no new variables; a comment that the chat alias no longer drives a filter step.

## 4. Sensitivity rules, unchanged

Dataset rows shown are those the viewer may see; a dataset's abstract goes to the reranker or "why this?" only if its sensitivity is in `CRSW_LLM_SENSITIVITIES`; passages only from files `access.may_download` would serve. The page's existing sentence on this extends to descriptions.

## 5. Documents to change in the same pass

- `docs/how-to-deposit.md` (and rebuild the `.docx`): the finding section describes two pages, Find data (filters and words) and Search by meaning or content.
- `docs/web-deposit/finding-and-reuse.md` §5: a builder line saying the question-to-filter step was retired on 22 October 2026 and why, and that meaning search over descriptions moved to the search page.
- `docs/web-deposit/runbook.md`: the "Ranking by meaning is off" entry now refers to the search page.
- Handbook §7 (`crsw-infrastructure`, separate PR): the sentence describing the find page's filters and plain-words question. One line; Neil will take it.

## 6. Not in scope

Embedding `spatial_note`, `notes` or `provider` into the dataset vector (r11 §7); passaging spreadsheets (excluded by design); any change to what the promoter writes; soft-boost ranking by inferred subject or place, which was considered and dropped with the Ask step.

## 7. Order

1 and 3 first (removal is the smaller change and clears the bug at the same time); then 2; then 5. The test to run at the end is the one that prompted this: on Search by meaning or content, *Court cases about women moved abroad and forced to work in brothels* should list `tah-ngo-incidents-2003` first in the Datasets group, and *How much unrest and killing was there in Brazil and its neighbours year by year?* should list `acled-latam-caribbean-weekly` first, with neither query sharing words with the records.
