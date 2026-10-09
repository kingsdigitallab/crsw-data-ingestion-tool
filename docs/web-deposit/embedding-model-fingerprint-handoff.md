# Handoff: detecting a change of embedding model behind the ARC alias

> **Status, 9 October 2026:** parts 1 and 2 built and committed, with two
> departures: the readers rank only the rows whose `model` matches the live
> fingerprint rather than refusing a mixed file (passages files are rebuilt
> one dataset at a time, so a mixed set is the normal state during a
> rebuild), and the web keeps the last good fingerprint when the listing is
> briefly unreachable. One addition: the promoter sweeps every passages file
> itself when it sees a new backend, on any run, so there is no operator
> step. Part 3 (sentinels) is not built. The runbook (section 4, "ARC change
> the embedding model") is the operator's view.

Written 9 October 2026 from a design conversation with Neil. Repo: `crsw-data-ingestion-tool`. This is a proposal for the Claude Code session to implement; stop at the checkpoints and wait for review, as `docs/web-deposit/plan.md` requires.

## Problem

Meaning-based search (`promoter/embed.py`, `promoter/passages.py`, `crsw_web/catalogue.py`, `crsw_web/passages.py`) stores one vector per dataset record and per text passage, embedded through the KCL LLM platform. Vectors from different embedding models are not comparable: a query embedded with model B scored against a corpus embedded with model A returns numbers that look like similarities and mean nothing. The search would appear to work while ranking at random.

The code already guards against this in two ways:

- `embed.build()` keeps a stored row only if its `model`, `cut` and `modified` match the current run; a mismatch re-embeds. `passages.py` is incremental by checksum and stores `model` and `cut` per row.
- `Catalogue.rank()` returns nothing if the query vector's length differs from the stored matrix. The VM's DuckDB copy of passages rebuilds if the configured `dims` differs from what it holds.

Both guards are defeated by the way ARC expose models. `CRSW_LLM_EMBED_MODEL` / `PROMOTER_LLM_EMBED_MODEL` is an alias, `arc:embedvl`. The stored `model` column holds that alias. If ARC repoint the alias to a different backend, the alias is unchanged, the rows all still "match", and nothing rebuilds. If the replacement has the same width as the old one after the `EMBED_DIMS` cut (currently 1024 from a native 2048), the length check passes too.

## What the platform tells us (verified 9 Oct 2026)

The embeddings reply only echoes the alias:

```
POST /embeddings  {"model":"arc:embedvl","input":"sentinel"}
→ model: arc:embedvl, dims: 2048
```

`GET /models` is LiteLLM's listing and carries the backend. The entry for the alias:

```
id:                   arc:embedvl
created:              1782483199
backend_model:        Qwen/Qwen3-VL-Embedding-2B
backend_model_size:   2B
backend_model_quant:  FP16
backend_model_type:   Embedding
mode:                 embedding
litellm_model_name:   arc:embedvl
```

There is a second embedding alias, `arc:embed` → `qwen/qwen3-embedding-0.6b`, FP16, text-only. Not in use; noted because the per-row `model` column already allows two aliases to coexist, and it may suit the passage layer later. The `backend_*` fields are LiteLLM's custom model info, not part of the OpenAI spec; field names could change.

## Proposed change

Two layers. The first is cheap and precise; the second is a fallback that depends on nothing but the embeddings endpoint behaving.

### 1. Fingerprint from `/models`

Add to `crsw_web/llm.py` (the only HTTP client to the platform, keep it that way):

```python
def embed_fingerprint(self) -> str:
    """What the embed alias currently resolves to, as one string, from
    GET /models. Falls back to the bare alias (and says so in the log)
    when the listing is unreachable or has no backend fields."""
```

Composition: `"{alias}|{backend_model}|{backend_model_quant}|{created}"`, e.g. `arc:embedvl|Qwen/Qwen3-VL-Embedding-2B|FP16|1782483199`. Include `created` deliberately: it should change if ARC re-register the alias with the same backend name (a weights or serving refresh). If the alias is missing from the listing or the fields are absent, return the alias alone and log a warning; never raise, never block a promotion over this.

Store the fingerprint in the existing `model` column of `embeddings.parquet` and of every `index/passages/<identifier>.parquet`, in place of the bare alias. The matching logic in `embed.build()` and `passages.build()` then does the right thing without change: a repointed alias gives a new fingerprint, every row mismatches, the next `promoter index --embed` and `promoter passages` runs rebuild. A new column is not needed; the `model` column was always meant to answer "what made this vector".

Consequence to accept: the first run after this change re-embeds everything once, because every stored row says `arc:embedvl` and the new fingerprint is longer. Say so in the commit and the runbook.

Call sites: `promoter/__main__.py` passes `cfg.llm_embed_model` into `embed.build()` and `passages.build()` (around lines 200, 236, 480). Replace with `platform.embed_fingerprint()` resolved once per run and logged (`embed_model_resolved` as a log line, so the audit trail shows what the vectors were made with).

### 2. Query-side refusal in the catalogue

`Catalogue._read_vectors()` discards the `model` column. Keep it: read the distinct `model` values from the file; if there is more than one, treat the file as unusable (log, no vectors) rather than ranking a mixed corpus. Record the single value on the catalogue.

In `Catalogue.rank()` (or wherever the query is embedded in `ask`), compare the stored fingerprint with `platform.embed_fingerprint()` (cached for the catalogue's `refresh_seconds`, not fetched per query). On mismatch return no ranking, and let `ask` degrade the way it already does on `PlatformError`: text search still runs, a notice says meaning-based ranking is off until the index is rebuilt. The passages copy (`crsw_web/passages.py`) should do the same at load: a `model` value in the Parquet that differs from the live fingerprint means refuse, not rebuild — the VM must not re-embed; only the promoter writes vectors.

Keep the existing length check as well; it is free.

### 3. Sentinel check (second line, smaller)

Independent of `/models`. A short fixed list of sentences in `crsw_deposit`? No — nothing in `crsw_deposit/` talks to a network; put it in `promoter/embed.py`:

```python
SENTINELS = (
    "A treaty prohibiting the slave trade, signed in 1956.",
    "Satellite imagery of mining sites in Colombia, 2019 to 2023.",
    "Interview transcripts with survivors of forced marriage in northern Uganda.",
    "Monthly counts of armed conflict events by province.",
)
```

At each run, embed the sentinels and store their vectors in a separate small file, `<index_prefix>/sentinels.parquet` (columns: `position`, `text`, `model`, `cut`, `dimension`, `embedding`), never in `embeddings.parquet`, which stays one row per dataset so no reader has to know to skip anything. On the next run, embed them again and compare cosine against the stored ones; anything under 0.98 (FP16 slack) means the model changed under a stable fingerprint, so log `embed_model_drift` with the scores and treat it as a mismatch (full rebuild, and rewrite `sentinels.parquet`). The only code that reads `sentinels.parquet` is this check in the promoter; the catalogue and the web service never open it. A missing file means first run: write it and carry on.

If this feels like too much for one pass, do 1 and 2 first and leave 3 as a follow-up; 1 and 2 close the alias problem on their own as long as LiteLLM keeps those fields.

## Config and docs

- `.env.example`: no new variables. Add a comment under `CRSW_LLM_EMBED_MODEL` that the stored vectors carry the resolved backend, and that a change of backend behind the alias triggers a full re-embed on the next promoter run.
- `docs/web-deposit/runbook.md`: a short section "If ARC change the embedding model" — what the operator sees (a long `embeddings_written` with `embedded` ≈ all rows; `embed_model_drift` if the sentinel fires; the find page's notice), and that the fix is to let the next run complete. Mention `arc:embed` as the other alias.
- `docs/web-deposit/finding-and-reuse.md` §5 and §7: a sentence each that vectors are keyed to the resolved backend, not the alias.
- Phase 6 decision record (when it exists): the model is a pinned dependency with an expected two or three forced migrations over the Centre's life, each a rebuild from stored text, not from files.

## Tests

- `tests/test_llm.py`: `embed_fingerprint()` against a fake `/models` body with the fields above; against a body missing the alias; against a transport error. All three return a string, only the first carries the backend.
- `tests/test_promoter_embed.py`: a stored file with `model = "arc:embedvl"` and a current fingerprint `arc:embedvl|…` re-embeds every row; a stored file with the matching fingerprint keeps them. `sentinels.parquet` is written on first run, left alone when scores hold, and a drifted sentinel forces a full re-embed. `embeddings.parquet` never contains a sentinel row.
- `tests/test_promoter_passages.py`: same keep/re-embed behaviour on the `model` column.
- `tests/test_web_catalogue.py` and `tests/test_web_ask.py`: a mixed-model file yields no vectors; a fingerprint mismatch yields no ranking and the ask response carries the notice.
- `tests/test_web_search.py`: passages copy refuses on a `model` mismatch and does not try to embed.

## Checkpoints

1. After `embed_fingerprint()` and its tests, before touching the promoter: show the fingerprint string the live platform returns.
2. After the promoter change, a `promoter index --embed --dry-run` (or equivalent) against the test prefix showing the planned full re-embed, before running it for real.
3. After the catalogue change, the find page with a deliberately wrong `CRSW_LLM_EMBED_MODEL` showing text search still works and the notice appears.
4. Sentinels, if done in this pass.

## Out of scope here, noted for later

- Switching the passage layer to `arc:embed` (smaller, text-only) if volume ever matters.
- Image embedding through `arc:embedvl` for the `parti` domain and image-only PDFs; `supports_vision` is true on the chat aliases nexus/lite/nano/chat as well, which would allow describing scanned pages rather than OCR.
- A "schema passage" for CSV/Parquet members (column names, types, sample values) so tabular datasets are findable by content. Belongs in `passages.py` but is a separate proposal.
- Asking ARC for versioned aliases (`arc:embedvl-2026-10`) and their deprecation notice period. If they offer these, pin production to a versioned alias and the fingerprint becomes belt and braces.
