# Handoff: shorter passages, 350 words to 150 with overlap

16 October 2026. A re-embed of every passage, one release (v0.13.0), one
promoter sweep. Follows `query-instruction-handoff.md`, whose result
section says why: with the instruction on, the Convention clause still
trails a short marriage passage, because it is one practice of five in
a 350-word passage. The probe record is `scripts/embed_probe_2026-10-16.txt`.

## Why, measured before any code

The probe now splits its own Convention passage the way the promoter
does (`python scripts/embed_probe.py arc:embed --split W/O`: pieces of
W words, each starting O words before the last ended) and scores the
same queries against every piece. Live form (question instructed,
pieces bare), scores at 1024, best piece of each split:

| passage size | verbatim sentence | paraphrase, no shared words | control |
|---|---|---|---|
| 348 words, whole (today) | 0.355 | 0.267 | 0.120 |
| 200 words, 40 overlap | 0.444 | 0.354 | 0.150 |
| **150 words, 30 overlap** | **0.542** | **0.401** | 0.163 |
| 100 words, 20 overlap | 0.511 | 0.418 | 0.154 |

The competitor on the web VM (Nepal CCPR opinion p13, about 120 words,
all about a woman, a husband and a marriage) scored 0.454 and 0.307 on
those two queries. Being short already, it will hardly move when
re-split. So at 150 words the Convention piece should lead both queries
with room to spare (0.542 against 0.454, 0.401 against 0.307), where
today it is second and seventh. 100 words gains 0.017 on the paraphrase,
loses 0.031 on the verbatim sentence and triples the row count; 200
words is not enough. **150 words with a 30-word overlap** (step 120).

The control query rises a little with shorter pieces (0.120 to 0.163):
every short text is a little closer to everything. The gap that matters,
real query to control, widens from 0.15 to 0.24 on the paraphrase.

## What changes

All in the promoter. The web reads whatever the passages files hold.

**`promoter/passages.py`**

- `PASSAGE_WORDS = 150`, `OVERLAP_WORDS = 30` (the new defaults;
  comment says why and points here).
- `build(...)` and `refresh(...)` take `words` and `overlap`. Every row
  gets a `split` column, the string `"150/30"`, so a file says how it was
  cut. `COLUMNS`, `parquet_bytes` (a string field) and `Summary` (for the
  log line) carry it.
- The unchanged-file check (`same`: checksum, model, cut) also requires
  the row's `split` to match. A file cut under another layout, or under
  none (every file in the bucket today has no `split` column), is read
  and split again. Nothing else forces a rebuild: the column is the
  switch, so a changed setting can never leave one dataset at 350 words
  and the next at 150 without the log saying so (`passages_written`
  shows `embedded` = everything, `kept` = 0, and `split`).
- Reuse of a vector by identical text (`reuse`) does **not** check
  `split`: the same text with the same model and cut has the same
  vector whatever layout produced it. A short file that gives one
  passage under both layouts keeps its vector and only its `split`
  changes. (This is why the rebuild re-embeds "nearly" everything,
  not everything.)
- `chunk()` itself is unchanged: it already takes `words` and
  `overlap`; `build` just stops calling it with the defaults.

**`promoter/config.py`**

- `passage_words: int = 150` (`PROMOTER_PASSAGE_WORDS`) and
  `passage_overlap: int = 30` (`PROMOTER_PASSAGE_OVERLAP`), read with
  the existing `opt_int`. `ConfigError` unless `words > overlap >= 0`
  and `words >= 20`. Settings, not just constants, so the next size trial
  is an env change and a sweep, not a release.

**`promoter/__main__.py`**

- The two callers of `refresh` (`_write_passages` after a run,
  `cmd_passages`) pass `cfg.passage_words` and `cfg.passage_overlap`.
  The `passages_written` line gains `split`. No new command, no flag: a
  changed layout is noticed per dataset by the column, and
  `python -m promoter passages` is already the sweep.

**`crsw_web/`: nothing.** The copy loads its own named columns from each
parquet file and ignores the rest, the vector length is unchanged, a
changed file is picked up by ETag within `CRSW_INDEX_REFRESH_SECONDS`,
`MIN_WORDS` (6) still applies, and the "Why this?" link's `position`
simply points at the new numbering once the copy refreshes.

**Not changed, deliberately:** the per-dataset cap (20,000 passages; at
150 words that is still three million words), the file size cap, the
`partial` rule, `SEARCH_TOP`, the `limit * 5` candidate pull and
`WEAK_BAND`. Two things to look at *after* the results, not before:
with a 20% overlap (was 14%) two neighbouring passages of one file can
both appear for one sentence, and shorter passages spread scores more,
so the 0.05 weak band may fold differently. Both are web-side tuning
with the numbers in hand; neither is this handoff.

## Tests (each watched red, then green)

`tests/test_promoter_passages.py`

- `build` with the defaults: full passages are 150 words, the next starts
  30 words before the last ended, every row has `split == "150/30"`;
  `parquet_bytes` round-trips the column.
- A file whose rows carry another `split` (and one whose rows carry none,
  as the bucket's do) is read again and re-embedded although checksum,
  model and cut all match: `files_kept == 0`, `embedded` = its passages.
- A one-passage file whose text is identical under both layouts keeps its
  vector (`kept == 1`, `embedded == 0`) and its row now says the new
  `split`.
- `passages_written` carries `split`; a run passes the configured values
  (`PROMOTER_PASSAGE_WORDS=40`, `PROMOTER_PASSAGE_OVERLAP=10` in the test
  env gives 40-word rows with `split == "40/10"`).

`tests/test_promoter_passages.py`, beside the existing config checks (the promoter's env tests for passages live there)

- Defaults 150 and 30; the env overrides; `overlap >= words` and
  `words < 20` are `ConfigError` with the variable named.

`tests/test_web_search.py`: nothing new unless a fixture builds a parquet
file by hand without the column, in which case it shows the web does not
need it. Full suite (`.venv/Scripts/python -m pytest`, about two and a
half minutes, in the background), `python -m unittest`,
`sh scripts/check-no-secrets.sh`.

## Documentation

- `.env.promoter.example`: the two settings with a dated comment (150/30
  since 16 October 2026, the probe table in one line, "changing either
  re-splits and re-embeds every file on the next `promoter passages`").
- `docs/web-deposit/runbook.md` §6 passages paragraph (line 293, "about
  350 words") and §7 "How passage search grows" (a passage count about
  three times today's; the per-passage memory figure stands).
- `docs/web-deposit/finding-and-reuse.md` §7: a dated note (16 October
  2026, passage length) above the instruction note, with the probe
  table's winning row and the result once measured; the "For the
  builder" line (350 with 50) updated.
- `CLAUDE.md` needs no change (it names the switches, not the size).
- Memory note after deploy.

## Release and deploy

Tag v0.13.0 when the user says (promoter behaviour and stored layout
change; the web gets the same tag for tidiness, same as every release).
Then, line by line as always:

1. Internal VM: pull the tag, `--profile internal build promoter`, then
   `$P passages --env-file /dev/null` (`P="docker compose -f deploy/compose.yaml
   --profile internal run --rm promoter python -m promoter"`). No `.env.promoter`
   change is needed: the defaults are the new values. Expect every
   dataset's `passages_written` line to show `split: "150/30"`,
   `embedded` close to its total and `kept` near zero, and the corpus to go
   from 304 passages to roughly 800–900 (22 files; one 348-word file gives
   three pieces instead of one). About thirty embed requests in all.
2. Web VM: pull the tag and `up -d --build` as for any release (export the
   port and proxy CIDRs first, as the runbook says). The copy refreshes
   itself within a minute; `/search.json` `stats.passages` shows the new
   count, `stats.current` equal to it.
3. The four queries again, meaning order and default order, beside the
   recorded ones:

| query | recorded (350 words) | want (150 words) |
|---|---|---|
| `A woman on the death of her husband is liable to be inherited by another person` | Convention p1 #2, 0.355, behind Nepal p13 0.454 | Convention first, about 0.54 |
| `widows passed on to a relative of the deceased` (meaning order) | Convention p1 #7, 0.270; best Nepal p13 0.307 | Convention in the top three, about 0.40; scgb p10 and p17 (widows pressured to remarry) near it |
| same, default order (reranker off) | as above, first spec file #10 folded | the same list; nothing new pinned |
| `satellite imagery of cropland` | best 0.282, spec files | still spec files, still well below the real queries |

Record them in the probe file and in this document's result section.

## Order against the other open handoff

`remove-from-store-handoff.md` (the six test datasets leaving the store)
is independent and should come **after** this one: the recorded
baselines were taken with those datasets present, and the comparison
above only means something on the same corpus. Once both are in, the
spec-file noise goes with the datasets.

## Not this change

The reranker (off on the web VM since 16 October; `arc:rerankvl` ranks
text as flatly as its embedding sibling), the weak band, and folding
neighbouring passages of one file: web-side, after the numbers. The
find page's question-to-filter step inventing a strand: own handoff.

## Result, 19 October 2026 (v0.13.0 live on both VMs)

The sweep on the internal VM walked 7 datasets and wrote 748 passages (was
304), 743 embedded and 5 kept (short texts identical under both layouts),
every row `150/30`, nothing failed. The web copy had them a minute later
(748 passages, 5 datasets, 22 files, all current). The image build on the
internal VM hit a stale build-cache snapshot on export; `docker system
prune -f` cleared it and the sweep ran on the new code.

| query | recorded (350 words) | got (150 words) |
|---|---|---|
| verbatim sentence | Convention #2, 0.355, behind Nepal p13 0.454 | **Convention p2 first, 0.627** (phrase match); Nepal p13 0.453 |
| paraphrase, meaning order | Convention #7, 0.270; best Nepal 0.307 | **Convention p2 first, 0.492**; scgb p17 (widows required to remarry) 0.406; scgb p10 (widowed women pressured to remarry) 0.337 |
| paraphrase, default order (reranker off) | Convention #7, first spec file #10 | the same list; no spec file in the top 10 |
| control | best 0.282, spec files | best 0.325, spec files; 0.17 below the paraphrase (was 0.03) |

The probe's prediction held with margin (0.542 predicted, 0.627 seen;
0.401 predicted, 0.492 seen). Full lists in `scripts/embed_probe_2026-10-16.txt`.

Two things seen on the way, both web-side and neither urgent. `WEAK_BAND`
(0.05) now folds the second result on the paraphrase, scgb p17 at 0.406,
as weak behind the Convention's 0.492: a passage a reader would want is
hidden. The band was set when every score sat within a few hundredths of
the best; with scores spread out, a fixed 0.05 below the best is too tight.
And on the verbatim query the Nepal passage appears twice as neighbours
(positions 64 and 65), the 20% overlap showing. Both are the next web-side
look, after `remove-from-store-handoff.md` if that goes first, since
removing the test datasets changes the corpus the band is judged on.
