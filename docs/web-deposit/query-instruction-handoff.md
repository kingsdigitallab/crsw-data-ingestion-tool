# Handoff: an instruction on the question, none on the passages

16 October 2026. Small change, one commit. Context in
`scripts/embed_probe_2026-10-16.txt` (the probe that justifies it) and
§7 of the deposit sheet in Neil's John Trajer folder.

## Why

Search inside content could not find a passage from one of its own
sentences. Cause, in order: `arc:embedvl` (Qwen3-VL-Embedding-2B) squeezes
all text pairs into a 0.28–0.47 band; switched both VMs to `arc:embed`
(Qwen3-Embedding-0.6B, 1024 native) by `.env` on 16 Oct and the promoter
re-embedded everything on its own. That got the verbatim sentence to #2.
Qwen3-Embedding is trained asymmetrically: a *query* carries an
instruction, a *document* does not. The probe shows the instruction lowers
the wrong answers far more than the right one (control 0.23 → 0.12, wrong
passage 0.19 → 0.08, right passage 0.42 → 0.36), which is the separation we
lack. Only the question's vector changes; nothing stored does.

## Change

`crsw_web/llm.py`, `Platform.embed`: add `instruction: Optional[str] = None`.
When set, each text is sent as

    "Instruct: {instruction}\nQuery: {text}"

(Qwen3-Embedding's documented form; `\n` literal, one line each). When
None, texts go as now. Keep batching, `_cut`, and every error path exactly
as they are. Add a module constant:

    QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"

and a setting `llm_query_instruction` (env `CRSW_LLM_QUERY_INSTRUCTION`,
default QUERY_INSTRUCTION; blank = off) so it can be switched off without
a deploy if it ever misbehaves against a future backend.

Callers:

| Call | Role | Instruction? |
|---|---|---|
| `crsw_web/app.py:371` `platform.embed([q])` (search inside content) | query | yes |
| `crsw_web/catalogue.py:470` `platform.embed([question])` (ask in your own words) | query | yes |
| `crsw_web/app.py:438` sentences of hit passages, for highlighting | document | **no** — they are compared against the (instructed) question vector, which is the intended asymmetry |
| `promoter/embed.py:184` dataset rows | document | no |
| `promoter/passages.py:266` passages | document | no |

So: two call sites gain `instruction=settings.llm_query_instruction`, three
are untouched. Nothing in the fingerprint, the index, or the passages files
changes; no re-embed; the web container is rebuilt and that is all.

## Tests

- `tests/test_llm.py`: one test that `embed(["x"], instruction="do y")`
  posts `"Instruct: do y\nQuery: x"` as the input, and that `instruction=None`
  posts `"x"` unchanged.
- `tests/test_web_search.py` and `tests/test_web_ask.py` already fake the
  platform (`FakePlatform.embed(texts, batch)`); give the fake the new
  keyword (`instruction=None`) so the signature matches, and assert in one
  search test and one ask test that the fake received the instructed form
  for the question and the bare form for the sentences.
- `python -m unittest` and `python -m pytest` green.

## After deploy, in the browser (meaning order, `/search.json`)

| query | before (bare arc:embed) | want |
|---|---|---|
| `A woman on the death of her husband is liable to be inherited by another person` | Convention p1 at #2, 0.415, behind Nyaya p13 0.464 | Convention p1 first |
| `widows passed on to a relative of the deceased` | not in top 5; spec files 0.33–0.36 | Convention p1 and ICC policy p29 in the top 3, nothing bold |
| `satellite imagery of cropland` | spec table ("sensor, satellite") 0.456 | whatever, as long as the real-query scores sit clearly above it |

Record the three results beside the probe output. If the paraphrase still
misses, the next lever is passage length (350 words → ~150 with overlap,
`promoter/passages.py` `PASSAGE_WORDS`), which *is* a re-embed; a separate
handoff.

## Not this change

The find page's question-to-filter step inventing a strand (rs2, rs3 on
two questions that named none) is a different defect in `Catalogue.ask`'s
filter prompt or parsing; own handoff.
