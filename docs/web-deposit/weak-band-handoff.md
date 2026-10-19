# Handoff: the weak band, re-chosen on a real corpus

19 October 2026, evening. Follows `passage-length-handoff.md` (v0.13.0)
and `remove-from-store-handoff.md` (v0.14.0/1). Built the same evening.

## What the band is

After ranking, a passage found by meaning alone is marked *weak* and
folded on the page (never dropped) when its score is more than
`WEAK_BAND` below the best meaning score; with any word match present,
every meaning-only result is weak. `WEAK_BAND = 0.05` was set in
September on a corpus of six tiny test datasets whose scores all sat
within a few hundredths of each other: 0.05 then separated something
from nothing.

## What the corpus is now

One dataset, `rs1/legal-sources/green/0_raw/international-instruments-sample`,
seven files, 4,609 passages at 150/30. 4,097 of them are one document,
the Ongwen trial judgment (CR2021_01026.pdf, 1,076 pages of findings,
testimony and footnotes). The embedding is `arc:embed`
(Qwen3-Embedding-0.6B, 1024 numbers, query instruction on).

## The four queries, read with the band in mind

Full lists in `scripts/embed_probe_2026-10-16.txt` (19 October, evening).

| query | best | what is relevant, and where the noise starts |
|---|---|---|
| verbatim Convention sentence | 0.629 (phrase) | the clause; then meaning-only testimony about LRA "wives" from 0.472 down, all weak by the word-match rule. Right. |
| paraphrase "widows passed on to a relative of the deceased" | 0.494 | **0.494** Convention clause; **0.481** Ongwen p810, "so-called widows ... forcibly assigned to another man", a perfect hit; **0.406** scgb p17 (widows required to remarry); then 0.397 a footnote of citations (noise), 0.377 relevant-ish (a widow released), 0.366 to 0.33 testimony fragments and citations, **0.337** scgb p10 (widowed women pressured to remarry) at #17. |
| same, default order | identical list (reranker off, no word match) | |
| control "satellite imagery of cropland" | 0.308 | nothing relevant: intelligence-report citations and photograph exhibits from the judgment. **All 20 are shown as strong**, because they all sit within 0.05 of 0.308. |

Two failures of the current rule, in opposite directions:

1. The paraphrase folds scgb p17 at 0.406 (0.088 below the best) and
   shows only two results, though three are plainly right.
2. The control shows twenty results as strong, because a band relative
   to the best cannot tell a good best from a bad one.

## The rule chosen

A meaning-only result is strong when **both** hold, otherwise weak:

- it is within `band` of the best meaning score (`band` = **0.10**);
- its score is at least `floor` (**0.35**).

Word matches present: every meaning-only result is weak, as before.
Weak is folded, never dropped, as before. On today's lists:

| query | strong under 0.10 / 0.35 |
|---|---|
| verbatim | the phrase match alone (unchanged) |
| paraphrase | 0.494, 0.481, 0.406, 0.397: three right, one footnote. 0.377 and the rest fold. |
| control | nothing: the page says "Nothing contains your words. These are the passages closest in meaning, which may not be close at all." and lists them folded open, which is the honest reading of a 0.308 best. |

Follow-up, the same evening (v0.15.1): seen live, the open list on the
control read as twenty results rather than as nothing, so the all-weak
page now folds too, under "N passages, the closest in meaning", with the
notice "Nothing contains your words, and nothing is close in meaning."
The order radios read "Exact matching text" and "By close meaning".

Why 0.10 rather than a proportion of the best: at 0.494 the two give
the same cut (80% = 0.395), and a fixed width is easier to say and to
set. Why 0.35: above both controls seen (0.325 on 16 October against
spec files, 0.308 today) and below every result a reader would want
except scgb p10 at 0.337, which sits under footnote noise anyway and is
not reachable by any band. The floor is a property of this embedding
model: a different alias or dimension cut moves it, which is why both
numbers are settings rather than constants.

## Settings

- `CRSW_SEARCH_WEAK_BAND` (default 0.10): how far below the best a
  meaning-only result may be and still show as strong.
- `CRSW_SEARCH_WEAK_FLOOR` (default 0.35): the least score shown as
  strong, whatever the best is. 0 switches the floor off.

Both in `crsw_web/config.py` beside the other search settings, in
`.env.example` with a dated comment, read in `search_passages`
(`crsw_web/app.py`) in place of the constant.

## Tests

`tests/test_web_search.py`: the existing fold test rewritten to the two
conditions with the settings given explicitly; a case where the best
is below the floor (every result weak, the "Nothing contains your
words" page); a case where the band would keep a result the floor
drops; the settings read from the environment with the defaults.

## Not this change

Neighbouring overlapping passages of one file showing twice (the
verbatim query's Nepal p13 pos64/pos65; now also Ongwen p804 pos3149
and pos3151); the reranker (stays off); the footnote passages of a
judgment scoring as prose (a text-quality question for the splitter,
not the band).
