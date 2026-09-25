# Finding, reusing and integrating deposited data: the choices

25 September 2026. Planning note written after the first real promotion on the
internal VM. Nothing here is built or decided; each section is a choice with a
recommendation. Read with the Data Governance Handbook (§7 catalogue, §8.5
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
still to do: a `/datasets` JSON endpoint the form's picker reads, once the
read key exists.

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

*For the builder:* `crsw_web/llm.py` as a thin `httpx` client, no vendor SDK;
`promoter index --embed`; index Parquet gains `embedding` (float32 list) and
`embedding_model`; ranking with DuckDB `array_cosine_similarity` or the `vss`
extension.

## 6. Sequence

1. **Now, with no external dependency.** Fix the "derived from" defect and
   build r8 steps 3 and 4. Add the index step to the promoter. Both are
   promoter and CLI work.
2. **One ask to eResearch.** A read-only key for the web VM covering the audit
   prefix, the index prefix and the four strand prefixes, excluding staging;
   confirmation of the groups header the proxy sends; and whether the proxy
   buffers or time-limits large responses. This one ask unblocks the
   administrator page, browse, download and the picker. In parallel, ask the
   Centre for the amber download rule and the amber-metadata answer.
3. **Then.** Browse, search and streamed download in the deposit service; the
   picker in the form; the administrator activity page.
4. **Then.** Question-to-filter and embeddings (section 5, A and B) once the
   platform credentials are in hand.
5. **Later.** The cdisaw-parquet datasets tab; the contributed-data pathway;
   answering; the catalogue platform harvesting the index.
