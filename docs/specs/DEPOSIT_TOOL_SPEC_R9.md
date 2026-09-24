# CRSW Deposit Tool — revision spec (r9): categories that change after deposit

Status: **proposal, not agreed.** Decisions below were taken with Neil on
24 September 2026 and are being built step by step; each is marked
*decided* so the rest of the document can be read as the reasoning.
Read alongside r2 §2 (vocabulary governance) and r8 (provenance), which
this extends. Nothing here changes the unit of description, the key
layout, the object labels or the members-first-record-last rule.

**Origin:** the subject vocabulary and the categories given to a dataset
at deposit are not fixed. After deposit it must be possible to add a
category to a dataset, remove one, merge two categories into one and
split one into several, with a record of what changed and when, and a
way to bring every dataset's record up to the current categories.
Changes are made by a small team of core staff with authority, and the
structure has to be enforced rather than left to hand-editing a JSON file.

**A word on words.** Earlier revisions use *reclassification* for moving
a dataset to a different strand, sensitivity or state, which mints a new
UUID (r5 Q1). This revision is about a dataset's *categories* only: its
`subject` terms and its `domain`. The dataset stays where it is and keeps
its UUID. We call that **recategorisation**.

---

## 0. What exists today, and why it is not enough

- A dataset's categories are the `subject` list and the `domain` code
  inside `dataset.<name>.json`. Neither appears in the object key or in
  the four object labels, so a category change never moves data. Only
  the record has to be rewritten.
- The vocabulary is a flat list of term slugs. It has no history: no
  renames, merges, splits or retired terms. Unknown terms are refused, so
  a record that still carries a retired term fails validation, and the
  CLI makes the depositor choose every subject again.
- The promoter is the only process with a key that can write to dataset
  prefixes. It has one command, `run`, which only sees staged deposits.
- The bucket is versioned, so every rewrite of a record keeps the old copy.

## 1. The choices

Each is a question, the options considered, and the decision.

### 1.1 Where does the history of a dataset's categories live?

- A. Rely on the bucket's object versioning. Free, but invisible: nothing
  says why or when a term changed, and history is lost if a record is
  ever copied elsewhere.
- B. A `category_history` list inside the record. Each entry says when,
  who, what changed, why and which vocabulary version applied. Travels
  with the record, shows in exports.
- C. A separate history object beside the record, or a central register
  of every dataset's categories. Two sources of truth; the copy in the
  store goes stale between regenerations; deposit itself would have to
  write to the register from a service that can only write to staging.

**Decided: B.** The record is the contract and the one file every
consumer reads. Object versioning stays as the safety net. A register
of all datasets can be printed from the promoter's listing whenever
wanted; it is a report, not a master.

### 1.2 How are merges, splits, renames and retirements expressed?

- A. In the vocabulary file itself, which becomes a **term authority
  file**: one entry per term ever approved, with its facet, label,
  status (current or retired), the date it came in and, when retired,
  what replaced it; plus a dated, append-only `changes` list. One place,
  reviewed the way terms are, applied to every dataset.
- B. Per dataset, by hand, every time. Every steward redoes the same
  mapping and nothing records the decision.

**Decided: A.** The vocabulary repo already has a review route. A merge
is a policy decision and should be made once.

### 1.3 What happens to a dataset when a term is split?

A merge or rename has one right answer; a split does not.

- A. Give the dataset every successor term and write a history entry
  marked for review. The record stays valid; a steward later removes the
  ones that do not apply (a per-dataset change, 1.4).
- B. Leave the old term. The record is invalid until a person acts.
- C. Refuse to run until every split is resolved by hand for every dataset.

**Decided: A.** No record is ever left invalid, and the review list
falls out of the history field.

### 1.4 How does a steward change one dataset's categories?

- A. A change file handed to the promoter by whoever operates it: which
  dataset, terms to add, terms to remove, a new domain, a reason.
  Dry-run first, every rewrite logged.
- B. A form in the web service that writes a change request into
  `staging/`, which the promoter picks up and applies exactly as it does
  for deposits. Self-service for stewards without the VPN, and no extra
  permission for the web service.
- C. Both, B built on A.

**Decided: A now, B later.** Same underlying functions. B is recorded in
the runbook's backlog and is not built until a steward asks for it.

### 1.5 Who may change the vocabulary, and how is structure enforced?

- A. A validator in the shared package that checks every rule of the
  authority file, run in the vocabulary repo's CI on each pull request
  and in the tool before a fetched file is trusted. Authority comes from
  the repo: branch protection, CODEOWNERS naming the stewards, one
  approval from someone other than the author.
- B. The promoter writes vocabulary changes back to git. This joins an
  unattended process holding a data key to the approved list, and a bug
  could rewrite the list with nobody looking.

**Decided: A. The promoter reads the vocabulary and never writes to
git.** Vocabulary changes are decisions that need review before they
touch any record. The promoter's write-back is to the store: it applies
an approved vocabulary to the records in place.

### 1.6 How do staff make a change without editing JSON?

- A. GitHub issue forms, one per change kind (add, rename, merge, split,
  retire, move), turned into a pull request by a GitHub Action that
  applies the change with the same library the validator uses. The
  steward approves in the browser; nobody types JSON.
- B. A command line over the same library (`crsw-vocab`) that edits a
  local checkout and validates.
- C. Both.

**Decided: C.** A is the route for staff; B is for whoever sets the
vocabulary up and for bulk work. Both exist only once the library does.

### 1.7 Which records get rewritten, and when?

- A. Only records the change affects: those carrying a term a vocabulary
  change touches, and those named in a change file. Untouched records
  keep the same bytes and get no new object version.
- B. Every record on every run, to stamp the current vocabulary version.

**Decided: A.** A rewrite is a new object version and a log line, and
both should mean something.

### 1.8 Can the vocabulary have sub-categories?

Categories may stay valid but gain sub-categories, or move up and down
a notional hierarchy, and this may never happen at all.

- A. Each term has an optional `broader` parent in the same facet. A file
  where no term has a parent is flat and behaves exactly as today. A new
  sub-category is an ordinary addition with a parent; the parent stays
  valid and datasets carrying it are untouched. A term moving up or down
  is a `move` change, which alters no record because the term itself is
  unchanged. Records store only the terms chosen, never their ancestors;
  "everything under X" is answered from the vocabulary at query time.
- B. Separate facet files per level, or terms that carry their whole path.
  Every move would then rewrite records.

**Decided: A.** Retiring a term that still has children is refused until
the children are moved.

### 1.9 Schema version

Adding an optional field is additive. **Decided:** fold `category_history`
into 0.6 if r8 is agreed before either ships; otherwise 0.7 with the same
read-and-upgrade path 0.5 has now.

### 1.10 Does the CLI use the same mapping?

**Decided: yes.** When an existing record's subjects are no longer all
current, the interview offers the mapped terms as the default instead of
forcing a full re-pick. Same function, no new interview step.

## 2. The authority file

```json
{
  "vocabulary_version": "2026-10-01",
  "domains": [ ... unchanged ... ],
  "activities": [ ... unchanged ... ],
  "terms": [
    {"slug": "forced-labour", "facet": "practices", "label": "Forced labour",
     "status": "current", "since": "2026-07-23"},
    {"slug": "debt-bondage", "facet": "practices", "label": "Debt bondage",
     "status": "retired", "since": "2026-07-23", "until": "2026-10-01",
     "replaced_by": ["forced-labour"]},
    {"slug": "bonded-labour-agriculture", "facet": "practices",
     "label": "Bonded labour in agriculture", "status": "current",
     "since": "2026-11-15", "broader": "forced-labour"}
  ],
  "changes": [
    {"date": "2026-10-01", "kind": "merge", "from": ["debt-bondage"],
     "to": ["forced-labour"], "by": "k1078591", "note": "steward decision, issue #12"},
    {"date": "2026-11-15", "kind": "add", "to": ["bonded-labour-agriculture"],
     "facet": "practices", "label": "Bonded labour in agriculture",
     "broader": "forced-labour", "by": "k1078591", "note": "issue #19"},
    {"date": "2026-12-02", "kind": "move", "from": ["bonded-labour-agriculture"],
     "broader": null, "by": "k1078591", "note": "promoted to top level, issue #23"}
  ],
  "facets": {"practices": ["forced-labour", "bonded-labour-agriculture"], ...}
}
```

`facets` is derived from the current terms and kept in the file so older
readers, and the CLI's subject listing, keep working; the validator
refuses a file where the two disagree. Change kinds: `add`, `rename`,
`merge`, `split`, `retire`, `move`. A `split`'s successors and a
`merge`'s target must already exist, so a split is an `add` per new term
followed by the `split`; the command line and the issue form do that in
one go.

Rules the validator enforces: slugs are lower-case words joined by
hyphens and unique across facets; every term has a facet, a label and a
status; a retired term has an `until` date and, unless plainly retired,
its successors; every slug a change names exists; changes are dated and
in order, and an existing entry is never edited or removed; a `broader`
parent is a current term in the same facet; no loops; a term with
current children cannot be retired, merged away or split.

## 3. The record

`category_history` joins the optional fields, after `provenance`. One
entry per thing that happened, oldest first:

| field | required | notes |
|---|---|---|
| `when` | yes | UTC timestamp, as `created` |
| `by` | yes | KCL username, or `vocabulary` when a vocabulary change was applied mechanically |
| `kind` | yes | `added`, `removed`, `replaced`, `split_review`, `domain_changed` |
| `from`, `to` | per kind | the terms (or domain codes) before and after |
| `reason` | no | the steward's reason, or "vocabulary change dated …" |
| `vocabulary_version` | no | the vocabulary the change was made against |

A rewritten record changes only `subject` and/or `domain`,
`vocabulary_version`, `modified` and `category_history`. Files, UUID,
`created` and the key never change. The exporter carries the history
under the local `crsw:categoryHistory` term.

## 4. Mapping a record forward

Replaying the `changes` list in date order over a subject list: a rename
or merge substitutes; a split gives every successor and reports
`split_review`; a retirement with no successor drops the term and reports
`removed`; `add` and `move` never alter a list. Order is kept, duplicates
collapse, and a list that is already current comes back unchanged with
nothing reported, so the promoter can run the mapping over every record
on every run and rewrite only those where something happened.

## 5. Build order and checkpoints

Stop at each and wait for review.

- **A. Shared package** (*built*): `crsw_deposit/authority.py` (validator,
  facets derivation, tree, apply a change, map subjects), the bundled
  vocabulary in the new shape, `category_history` in `record.py` with
  `apply_dataset_change` and `apply_vocabulary_mapping`, schema and
  mapping rows, tests. *Checkpoint: the bundled file validates; merge,
  split and retire map a subject list as decided and mapping twice is a
  no-op; a record with history validates and exports; `--dry-run`
  output unchanged.*
- **B. Vocabulary command line** (*built*): `crsw-vocab validate | add | rename |
  merge | split | retire | move | tree`.
- **C. Promoter**: dataset listing, `recategorise` (dry-run, change file,
  logging), stale-term mapping at promotion time.
- **D. CLI**: mapped terms as the default on re-deposit; indented listing
  when the vocabulary has a hierarchy.
- **E. Vocabulary repo starter kit**: README, CODEOWNERS, issue forms,
  validation workflow, apply-issue workflow, delivered in this repo under
  `docs/vocabulary-repo/` for when `crsw-vocabulary` is created.
- **F. Docs**: README, runbook, the deferred web route.

## 6. What this does not do

- Move a dataset to another strand, sensitivity or state.
- Touch members, labels or keys.
- Give the web service any new permission.
- Create the vocabulary repo. That is a centre or eResearch action, and
  until it exists every tool falls through to the bundled file, which is
  now itself an authority file.
