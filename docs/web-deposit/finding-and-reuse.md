# Finding, reusing and integrating deposited data: the choices

25 September 2026. Planning note written after the first real promotion on the
internal VM. Each section is a choice with a recommendation; a builder line at
the end of a section says what has since been built and when (sections 1 to 3
and 5 as of 26 September; section 7, searching inside documents, added the
same day and next to build). Read with the Data Governance Handbook (§7 catalogue, §8.5
egress), the r8 provenance spec, and the phase 4 promotion note.

Three needs prompted it:

- (a) Nothing yet lets a researcher search, find, retrieve or download what has
  been deposited.
- (b) A researcher depositing an interim or final dataset should be able to find
  the dataset it came from, already in the store, and refer to it.
- (c) New deposits might be made findable, and perhaps queryable, alongside the
  (CD)ISaW harmonised corpus served by cdisaw-parquet, without blurring which
  datasets are the core corpus.

## What exists today

**Fixed by earlier decisions, not reopened here.** The dataset record in the
bucket is the ground truth; any catalogue is regenerated from it. The catalogue
platform is chosen by the governance council and is not chosen yet. Lineage
(what was done to data) is a separate concern from the catalogue (what exists).
The web deposit service holds a key that can write only to staging; the
promoter holds the real key. Green data can be taken freely; amber access goes
through the dataset's steward; red data never enters the store.

**In the deposit tool.** The record already has a structured "derived from"
field: a list of references, each either another dataset in the store (by
identifier, with its UUID and version) or something outside (a URL or
citation). It is mapped to Dublin Core and PROV, so the export already says
"this came from that". The promoter is specified to fill in the parent's UUID
and version at promotion time (r8 §4), but that step, the CLI question and the
web form field are not built (r8 steps 3 to 5). There is a defect to fix
first: the CLI's "parent object key" question and the web form's handling both
store the answer as plain text, which the validator then rejects, and the
browser form never sends the field at all. Listing what is in the store exists
only on the promoter side (`promoter datasets`); the web service cannot read
datasets with the key it has. The place for a read path was designed in: a
second, read-only key, a per-prefix permission check, and no change to the
upload handlers. The deferred administrator page needs the same key.

**In cdisaw-parquet.** It is not a catalogue. It is a query service over the
rows of one harmonised corpus (54 sources, about 22 million events) held as
Parquet on Ceph and served by DuckDB from a single container behind a shared
password. It has facets over event rows, a registry of sources and a manifest,
and its explorer already has a dataset picker. It imports the deposit tool's
conventions package and writes a record per source, but into a single test
prefix rather than the real layout, and its raw source files were never
deposited to the raw state, so its "derived from" points at publishers' URLs
rather than at anything in the store.

## 1. Where should "find and download" live?

**Question.** A researcher, possibly at Nottingham without VPN, wants to find a
dataset and download it. Which piece of software does that?

- A. A third service, in a new repository on a new VM, with a read-only key.
- B. A read role in the existing deposit service: same image, same repository,
  a second read-only key, new pages behind the same sign-in proxy.
- C. Inside cdisaw-parquet.
- D. Wait for the catalogue platform.

**Recommend B.** The seam for it was designed in from the start, the KCL proxy
already provides sign-in and group membership, and the promoter already has
the code that lists datasets. C is the wrong shape: cdisaw-parquet searches
rows of one corpus behind one shared password, not datasets. D waits on a
council decision with no date, and nothing built under B pre-empts the
platform, because it is all regenerated from the records. A is the fallback
if eResearch will not widen the web VM's key: the same code then runs on a
second VM with a different configuration, which the compose profiles already
allow.

**How downloads travel.** A pre-signed link would be minted by the service but
fetched by the browser straight from the Ceph gateway, which is reachable only
on the KCL network or VPN. That does not help the Nottingham researchers the
service exists for. So the default is to stream the download through the
service: Ceph to service to browser, through the proxy, the mirror of the
streaming upload already agreed. The bytes never reside on the VM: one chunk
in memory at a time, nothing on disk, no size limit beyond bandwidth. Range
requests are passed through so a large per-file download can resume. A whole
dataset can be streamed as a zip built file by file, which cannot resume, so
per-file links are the primary route and the zip a convenience. Pre-signed
links remain a configuration switch for VPN users and very large transfers.
One question for eResearch: whether the KCL reverse proxy buffers responses or
limits transfer time (uploads already pass through it at the agreed cap).

**Who may download what.** Green: any signed-in Centre member. Amber: either
membership of the strand's group as reported by the proxy, or a list the
steward maintains. Red never appears. *The amber rule is a Centre policy
decision to make before the download page goes live, not a technical one.*

*For the builder:* built 25 Sept (find and browse): `crsw_web/config.py`
read keys and `CRSW_AMBER_ACCESS`; `crsw_web/s3.py::ReadOnly`;
`crsw_web/access.py::may_download`; `crsw_web/catalogue.py` over the index;
routes in `crsw_web/app.py`; the streamed download route with the Range
header passed through, and the nginx download limits. Not built: the
whole-dataset zip (per-file is the primary route; add when asked).

## 2. What does search read: the bucket every time, or an index?

**Question.** Search needs the title, abstract, subjects, dates and lineage of
every dataset. Where does it get them?

- A. Walk the bucket and open every record on each request (what
  `promoter datasets` does today).
- B. The promoter writes an index after every run: one row per record, kept
  in the bucket in two forms, JSON lines and Parquet. The bucket stays the
  truth; the index is a cache anyone can rebuild.
- C. A database inside the web service.

**Recommend B.** Walking is fine for tens of datasets and slow for thousands.
A database is state to back up and a second truth that can drift. B costs one
promoter step, scales to thousands of datasets, and the Parquet copy is
directly queryable by DuckDB, which is how cdisaw-parquet, a researcher's
laptop and a future catalogue harvester would all read it. Search in the web
page is then a filter over one file, with no search engine to run. Full-text
search over the contents of data files stays out of scope (it is a separate
scoping item, K12).

The index row carries what the record carries: identifier, UUID, strand,
project, sensitivity, state, dataset, domain, title, abstract, subjects,
coverage dates, version, depositor, created, modified, file count and bytes,
schema and vocabulary versions, the "derived from" references, and the tool
named in provenance. It also carries an origin column (deposit tool, CLI,
cdisaw-parquet) so a reader can tell the harmonised corpus from everything
else.

*For the builder:* built 25 Sept: `promoter/index.py`, written by `run` and
`recategorise` after a change and by `promoter index` on demand;
`index/datasets.jsonl` and `index/datasets.parquet` (pyarrow in the web
extras). Runbook section 6 describes the columns.

## 3. How does a depositor refer to the dataset theirs came from?

**Question.** Someone depositing a cleaned or final dataset wants to say which
raw or interim dataset it was made from. How do they identify it?

- A. Type the five-part identifier. The promoter looks the parent up, fills in
  its UUID and version, and warns if it cannot find it.
- B. Pick it from a list in the form, fed by the index (needs the read-only
  key on the web VM).
- C. A "promote to next state" command that copies a raw or interim dataset
  forward and writes the reference itself.

**Recommend B as the main route, fed by the index from section 2, so the
identifier is found rather than guessed or remembered.** A is the CLI form
and the fallback when the index is unavailable. C is worth building when a
strand's workflow is raw, then interim, then final in a regular rhythm; not
before. A and B fill the same field, so the record does not change between
them. The prerequisite for both is the defect above: both routes must turn the
answer into a structured reference, and the browser must send the field.

The reverse question, "what has been derived from this dataset", is a query
over the index's "derived from" column. That is the catalogue's job and comes
free once the index exists.

*For the builder:* r8 steps 3 and 4 built 25 Sept (`record.references_from_lines`
shared by `deposit.py` and `crsw_web/metadata.py`; `promoter/resolve.py`);
the picker (built 25 Sept, `/datasets.json` and the "Find a dataset in
the store" panel under the form's derived-from box, shown only when the
read role is on).

## 4. How do deposits meet cdisaw-parquet, keeping the core corpus distinct?

**Question.** "Integrate into cdisaw-parquet's search" can mean two different
things. Which, and how are the core sources kept identifiable?

- (i) *Findable alongside.* The explorer shows deposited datasets (title,
  abstract, subjects, a link to download) next to its own sources. It reads
  the index Parquet from section 2. The core corpus is distinguishable by its
  project prefix and by the origin column, so nothing is mixed.
- (ii) *Queryable row by row.* A deposited dataset appears in the explorer's
  maps and counts. That is only possible for data in the explorer's event
  shape. The pathway would be: the depositor declares that the dataset
  conforms to a named profile, cdisaw-parquet checks the files against it, and
  conforming data is read from a separate "contributed" area with its own
  dataset slug and origin, so the core sources are never mixed in.

**Recommend (i) once the index exists, and (ii) later, opt-in, with the
deposit tool never learning the explorer's schema.** Conformance is
cdisaw-parquet's check, run against the deposited files, not a rule in the
deposit tool.

Two housekeeping items make cdisaw-parquet the first real user of section 3:
deposit its records into the real layout rather than the single test prefix,
and deposit its raw source files to the raw state so its "derived from" can
point inside the store instead of at publishers' websites.

*For the builder:* a datasets tab in `web/demo.html` reading
`s3://crsw/index/datasets.parquet` through the existing DuckDB connection; an
`origin` value in the manifest; profile check as a `cdisaw-parquet conform`
subcommand.

## 5. Natural-language search over the index

**Question.** The KCL LLM platform offers OpenAI-style endpoints. Can a
researcher ask a question in plain words and find datasets?

- A. Question to filter. The model is given the index's columns, the
  vocabulary and the question, and returns a filter (strand, project, state,
  subjects, date range, words to match) that the ordinary search applies. The
  user sees the filter it chose and can adjust it.
- B. Meaning-based matching. Each record's title, abstract and subjects are
  embedded when the index is built; the vector is stored as a column of the
  index Parquet; results are ranked by similarity. Handles questions the
  vocabulary does not cover.
- C. Answering. A or B selects records, and the model writes an answer that
  cites dataset identifiers.
- D. Question to query over cdisaw-parquet's event rows. The same adapter, but
  a cdisaw-parquet feature, not a deposit-tool one.

**Recommend A and B first, C once A and B are trusted, D on the cdisaw side.**
The index is small, structured and metadata-only, which is the easy case for
this. Constraints to hold: only record metadata is ever sent to the platform,
never the contents of data files; the adapter is a configuration seam (base
URL, key, chat model, embedding model) with an off setting so the service runs
without it; embeddings are computed when the index is built, on the promoter,
off the request path, which means the promoter VM needs outbound access to the
platform host. One question for the Centre: may amber *metadata* (titles and
abstracts) be sent to a KCL-hosted model? It does not leave KCL, so likely
yes, but it is theirs to answer.

*For the builder (built 26 September, A and B; C not yet):* `crsw_web/llm.py`
is the thin `httpx` client (embed, question-to-filter, rerank; every failure
one error type so callers fall back). `promoter/embed.py` writes one vector
per dataset to `index/embeddings.parquet`, a file of its own beside the index
so it can be dropped or rebuilt alone, incremental by identifier, modified
stamp and model. "Model" is not the alias but what the platform resolves it
to (`Platform.embed_fingerprint`, from `GET /models`), so a backend swapped
behind `arc:embedvl` re-embeds everything and the catalogue ranks only
vectors made with the backend of the day (9 October; the handoff note
`embedding-model-fingerprint-handoff.md` has the reasoning). Vectors are
cut to their first 1,024 numbers and stored at half precision
(`PROMOTER_LLM_EMBED_DIMS`), a quarter of the storage.
`Catalogue.ask` runs filter, meaning and rerank in turn, each degrading on its
own; the page shows "Understood as" with the filter as the filled-in form. Two
rules from the first live run: a dataset named in the question comes first,
and a filter that matches nothing relaxes to the closest datasets with a
notice. Ranking is in memory (NumPy), which is right for one vector per
dataset; section 7 says what changes for passages.

*9 October, the vocabulary's hierarchy:* a subject filter, typed or chosen by
the model, matches the term and every term under it in the vocabulary of the
moment (`authority.expand_subject`, asked of the app's vocabulary cache at
query time, so a merged change counts without a restart), and the count line
says so. A retired slug finds its successors with a notice, since records
never carry one. The chat model is given the subject terms as a tree,
indented, with scope notes where the vocabulary has them, and may name a
parent for a broad question. `/datasets.json` carries `subject_filter`
(asked, matched, how) so the form's picker and any other reader see the
same expansion. One known divergence: cdisaw-parquet's datasets tab reads
`index/datasets.parquet` and filters by exact slug, so it does not follow
the hierarchy until it reads `/datasets.json` instead. The handoff note
`vocabulary-hierarchy-and-scope-notes-handoff.md` has the reasoning.

*9 October, scope notes:* a term may carry `scope` (what it covers and
excludes, the source of the definition) and `aliases` (other spellings),
set by `crsw-vocab describe` or the vocabulary repo's "Describe a term"
form with no change entry and no new version, since description is not
identity. The deposit form shows the note under the term ("what this
covers", a `<details>`, no script) and as hover text; the CLI prints it
for `?term` and accepts an alias, saying which term it used; the dataset
page shows each subject as label, slug and note, linked to the filter;
the chat model's term list carries the notes, the single biggest help to
its choice. The embedded text names each subject by label and note, one
line each after the abstract so a long note cannot swamp a short one,
and every vector is keyed to the text it was made from (`text_sha`): a
note edited later re-embeds exactly the rows carrying that term, on the
next promoter run, a quiet one included (the quiet run reads the index in
place, one GET, not every record). The first run after this version
re-embeds everything once, for the fingerprint and the text together.

*14 October, the dropdown and the cost of a page:* the find page's subject
select now lists the whole vocabulary tree (every current term, indented
under its parent), not only the terms in use plus their parents: with
the live corpus the old rule showed none of the five contexts under
`armed-conflict` while the count line said "(and 5 narrower terms)", and
Neil preferred to see every term over never seeing an empty one. The
count line now names only the narrower terms the results carry, so it
cannot promise what the dropdown does not show; the chat model is still
offered the in-use terms plus their parents (`Catalogue.subject_tree`),
since it must pick something that can match; `subject_filter.matched`
keeps the full list searched. The same day, after a read-path audit: the
index and embeddings are re-read with `If-None-Match` (an unchanged file
is a 304, not a download and a parse), the vocabulary refresh runs on a
background thread instead of inside the first request after the
interval, the record route fetches once, the upload handler's storage
calls run in the threadpool rather than on the event loop, the passages
counts are remembered between syncs, and the sidecar compresses pages
and keeps its upstream connections. The promoter, when `GET /models`
fails, now says `embed_model_unresolved` and writes nothing rather than
keying vectors to the bare alias, and a quiet run embeds datasets missing
from the file (`embed_rows_missing`).

*21 October, places and providers (r11):* a record's `spatial` is a list
of UN M49 codes (`m49:419`, `iso:CO`) from `places.json`, read through
the vocabulary's own fetch → cache → bundled chain, and the find page
gains a Place filter that follows the hierarchy both ways
(`places.expand_place`: a query for Colombia finds a dataset tagged with
Latin America and the Caribbean or with the world, and a query for the
Americas finds one tagged with a country inside it), with the count
line saying so and `/datasets.json` carrying `place_filter` (asked,
matched, how). The chat model is offered the places in use plus what
contains them, indented like the subjects, and may answer with a
`place` code. The index carries `provider`, `spatial` and
`spatial_note` as recorded; expansion is query-time, as for subjects.
The dataset page names the provider (linked to its URL from the
vocabulary's `providers` block) and the places by label. The embedded
text does not yet include the place note (every vector would be re-cut
once); r11 §7 lists it as a follow-up.

**Switches, so any part can be turned off without touching code.** Each
function is off when its model name is blank (`CRSW_LLM_CHAT_MODEL`,
`CRSW_LLM_EMBED_MODEL`, `CRSW_LLM_RERANK_MODEL`; `PROMOTER_LLM_EMBED_MODEL`),
and the whole platform is off when the URL or key is blank. Which
sensitivities' metadata may go to the platform is one list on each side,
`PROMOTER_LLM_SENSITIVITIES` and `CRSW_LLM_SENSITIVITIES`, green by default:
the Centre's answer on amber is a one-word change, not a build. The same
lists will govern file contents when section 7 is built.

**22 October 2026, the list as projects.** With the first real deposits
in place the flat table (one row per dataset, the whole identifier in a
"Where" column) was reviewed against what a reader wants first: the
project. The find page now folds the rows into projects
(`catalogue.group_projects`, in order of first appearance so a
newest-first list and a question's ranking both survive), each a row
that opens to its datasets with state, depositor, files, size and date;
closed while browsing, open once a search, filter or question narrowed
the list. Ten projects a page (`page`, `per` of 10, 25 or 50; a size
chosen in the address is remembered in a cookie, so no script is
needed). `/datasets.json` stays unpaged: the form's pickers read every
row of a strand from it.

## 6. Sequence

1. **Now, with no external dependency.** Fix the "derived from" defect and
   build r8 steps 3 and 4. Add the index step to the promoter. Both are
   promoter and CLI work.
2. **One ask to eResearch.** A read-only key for the web VM covering the audit
   prefix, the index prefix and the four strand prefixes, excluding staging;
   confirmation of the groups header the proxy sends (*16 October 2026:*
   identity arrived as `x-webauth-user`/`-name`/`-mail` with no groups
   header, so `CRSW_AMBER_ACCESS=groups` still cannot be used); and whether the proxy
   buffers or time-limits large responses. This one ask unblocks the
   administrator page, browse, download and the picker. In parallel, ask the
   Centre for the amber download rule and the amber-metadata answer.
3. **Then.** Browse, search and streamed download in the deposit service; the
   picker in the form; the administrator activity page.
4. **Then.** Question-to-filter and embeddings (section 5, A and B) once the
   platform credentials are in hand. *Built 26 September.*
5. **Next.** Searching inside documents (section 7): the promoter's passage
   step first, then the VM copy and the search mode, both behind switches, on
   green data until the Centre answers on amber. *Built 26 September, off by
   default on both VMs until switched on.*
6. **Later.** The cdisaw-parquet datasets tab; the contributed-data pathway;
   answering; the catalogue platform harvesting the index.

## 7. Searching inside documents

*16 October 2026, later, passage length:* with the instruction on, the
Convention clause still trailed a short marriage passage, second on its
own sentence (0.355 against 0.454) and seventh on the paraphrase (0.270
against 0.307), because it was one practice of five in a 350-word
passage. The probe's split mode scored the same queries against pieces of
that passage before any re-embed: at 150 words with a 30-word overlap the
right piece scores 0.542 and 0.401, above the competitor, which is short
already and will hardly move; 100 words barely helps and triples the
rows, 200 is not enough. So passages are now 150 words, each starting 30
before the last ended (`PROMOTER_PASSAGE_WORDS`, `PROMOTER_PASSAGE_OVERLAP`),
every row records the layout it was cut under, and a file whose rows say
another is read and split again on the next run, so the change is one
`promoter passages` sweep that re-embeds nearly everything (identical
short texts keep their vector). The web is untouched; it reads whatever
the files hold (`docs/web-deposit/passage-length-handoff.md`). Result, 19
October 2026, both VMs on v0.13.0, 748 passages from 304: the Convention
clause is first on its own sentence (0.627) and on the paraphrase (0.492),
with the two widow passages of the scgb report second and third; the
control stays 0.17 below. The weak band (0.05 below the best) then folded a
relevant 0.406 result; re-chosen that evening on the sample grown to 4,609
passages as band 0.10 plus a floor of 0.35, both settings
(`docs/web-deposit/weak-band-handoff.md`): the paraphrase shows its three
right answers, the control shows nothing as strong. Later that day the
six test datasets were taken out of the store (r10, `promoter remove`,
`docs/web-deposit/remove-from-store-handoff.md`): one dataset, five
files, 512 passages remain, none of them this repo's own spec files.

*16 October 2026, the embedding model and the question's instruction:*
search inside content could not find a passage from one of its own
sentences. The probe (`scripts/embed_probe_2026-10-16.txt`) showed why:
`arc:embedvl` scores every pair of texts in one narrow band, 0.28 to
0.47, so nothing separates. Both VMs moved to `arc:embed`
(Qwen3-Embedding-0.6B, text only, 1024 native) by `.env` the same day and
the promoter re-embedded everything on its own; the verbatim sentence
rose to second. Qwen3-Embedding is trained asymmetrically, an instruction
on the query and none on the documents, and the probe shows the
instruction lowers the wrong answers far more than the right one (control
0.23 to 0.12, wrong passage 0.19 to 0.08, right passage 0.42 to 0.36). So
the question now goes to the platform as `Instruct: … Query: …`
(`CRSW_LLM_QUERY_INSTRUCTION`, blank = off) and nothing stored changes;
the sentences compared against it for highlighting go bare, which is the
intended asymmetry (`docs/web-deposit/query-instruction-handoff.md`). If a
paraphrase still misses, the next lever is passage length, 350 words to
about 150 with overlap, which is a re-embed and a handoff of its own.

*15 October 2026, how this grows:* the four seconds a live search takes
are the two platform calls (embed the question, rerank twenty passages),
which do not grow with the corpus; the scan that scores every passage is
linear and microseconds today. The first thing to grow is memory on the
web VM, where each of the two workers holds the whole passages table,
about 4 KB a passage. The runbook (§7, "How passage search grows") has
the ceilings, what to watch and the ladder: shorter vectors (both
`*_EMBED_DIMS` settings together, with a quality check first), then an
in-process HNSW index, then a vector store. Nothing to do until the
table nears 100,000 passages, which is thousands of datasets away.

**Question.** Sections 1 to 5 find datasets by their descriptions. The store
will hold journal articles, legal transcripts and survivor narratives. Can a
researcher search what those documents say, not just what the record says
about them?

This crosses the line held so far, that file contents never leave the store.
The platform is KCL-hosted, so the text never leaves the university, but it
is a change the Centre must agree to, separately for green and amber. The
design below is built so it can run on green alone, and so any part of it
can be switched off.

- A. Passages embedded at ingest, searched on the VM. After promotion the
  promoter reads each text-bearing file (plain text, Markdown, PDF, Word),
  splits the text into passages of about 150 words (350 until 16 October
  2026), embeds them, and writes
  `passages.parquet` beside the dataset's record: file path, position, a
  short excerpt and the vector. Incremental by file checksum, which the record
  already holds, so a re-deposit only touches changed files. The web service
  keeps a copy on the VM's disk arranged for nearest-neighbour lookup and
  offers "search inside the documents": a passage, its file and its dataset,
  with a link to download the file.
- B. Passages embedded at ingest, searched straight from the bucket. The same
  files, but each question reads the Parquet from Ceph. Simplest; too slow
  past a few hundred documents because every question pulls every vector over
  the network.
- C. Search at question time. Files are read and embedded when someone asks.
  No storage, but minutes per question and every question re-sends contents
  to the platform. Not workable.
- D. A separate search service. Everything in A, in its own container with a
  dedicated vector database. The step up from A when the passage count is in
  the millions; not needed to start.

**Recommend A, with D as the known next step if scale demands it.** Sizes,
so expectations are set: one passage vector is 2 KB at half precision and
1,024 numbers; a 300-page transcript is about 400 passages, so under 1 MB;
100,000 passages (250 such transcripts) is under a gigabyte on the VM
including the lookup index, and a million passages (2,500 transcripts, or
25,000 articles) about 4 GB. Ceph holds the durable copy under each dataset;
the VM copy is disposable and rebuilt from Ceph when missing. The slow part is
not embedding but getting text out of files: born-digital PDF and Word extract
at hundreds of pages a second, scanned documents need OCR, a hundred times
slower and a decision of its own. The first build skips image-only files,
counts them and reports them in the promoter's log so the size of that
problem is known before it is taken on.

*20 October 2026 (v0.16.0):* taken on in the smallest form. With
`PROMOTER_LLM_OCR_MODEL` set, a scan's first `PROMOTER_OCR_PAGES` pages
(two by default) are rendered and read through the platform's
vision-capable chat alias, one call a page, so the file is findable by
its title, author and opening; the rest of the document is not read. No
OCR engine on the VM (`docs/web-deposit/ocr-handoff.md`).

What a researcher sees is a passage of a file, which is content, not a
description, so a passage is shown only to someone the amber rule would let
download the file: the same `may_download` check as the download route,
nothing new. Amber transcripts and narratives will therefore be searchable
only once `CRSW_AMBER_ACCESS` is decided, however the embedding question is
answered.

**The switches.** `PROMOTER_PASSAGES` on or off; `PROMOTER_LLM_SENSITIVITIES`
says whose contents may be embedded at all (green only until the Centre
allows amber); a per-record opt-out for datasets whose contents should never
be indexed; `CRSW_PASSAGES` on or off for the search mode; and a cap on file
size and passage count per dataset so a mistaken deposit cannot fill the VM.

**Two things held fixed.** Embedding is incremental and keyed on the file's
checksum, so nothing is embedded twice. The passage index, on Ceph and on the
VM, is a cache like the dataset index: rebuildable from the files with one
promoter command, never the truth.

*For the builder (built 26 September, both sides):* `promoter/passages.py` reads plain text, Markdown, PDF
(text layer, `pypdf`) and Word (`python-docx`), splits into passages of 150
words with a 30-word overlap (350/50 until 16 October 2026; each row says
which, in `split`) and the page each starts on, embeds them and
writes `index/passages/<identifier>.parquet`. One departure from the choice
above: the file sits under the index prefix, not beside the record, so a
dataset prefix holds only what its depositor put there and the web VM's
read-scoped key over `index/` covers it without a wider grant. Incremental by
the record's checksum per file and the resolved embedding model (as for
section 5; a run that finds a new backend sweeps every passages file); a file
read and found to have no text leaves one marker row so it is not fetched
again (or, with `PROMOTER_LLM_OCR_MODEL` set, has its first pages read
as images). Switches: `PROMOTER_PASSAGES` (off
by default), `PROMOTER_LLM_SENSITIVITIES`, `PROMOTER_PASSAGES_EXCLUDE`, and
caps `PROMOTER_PASSAGES_MAX_FILE_BYTES` / `_MAX_PER_DATASET`. A promotion
runs it for the datasets it promoted; `promoter passages [--dataset ID |
--strand rsN]` rebuilds. On the web side `crsw_web/passages.py` keeps the
copy in one DuckDB file on the `passages` volume (`CRSW_PASSAGES_PATH`),
refreshed on the index cycle by ETag so only changed files are fetched, marker
rows left out, and searched with DuckDB's fixed-size array cosine similarity
rather than the `vss` extension: no extension to install means no egress the
VM may lack, and a full scan is milliseconds at a hundred thousand passages;
`vss` is the step up past a million. `/search` (and `/search.json`) embeds
the question, takes the closest passages, keeps only those whose file
`access.may_download` would serve this person (with `CRSW_AMBER_ACCESS=off`
amber rows are not even queried), reranks the top twenty (passages of the
allowed sensitivities only) and shows dataset, file with a download link,
page and the passage. Switch: `CRSW_PASSAGES` (off by default; needs the read
role and the platform). Answering (section 5 C) follows: the model writes a
short answer from the top passages with citations to file and page.
