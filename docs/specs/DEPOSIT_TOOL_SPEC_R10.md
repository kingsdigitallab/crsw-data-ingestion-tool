# CRSW Deposit Tool — revision spec (r10): the store changes only through the promoter

Status: **proposal, not agreed.** Decided with Neil on 16 October 2026
and built on 19 October 2026 (`promoter remove`). Read alongside r9,
whose rule this generalises. Nothing here changes the unit of
description, the key layout, the object labels or the
members-first-record-last rule.

**Origin:** test datasets from the first months were still in place and
padding search results; removing one by hand was five careful steps
across two machines (delete every object version, rebuild the index and
embeddings, sweep the passages files, keep the audit honest, do it where
the right key is), each a chance to get it wrong.

## 1. The principle

r9 said *categories change only through the promoter*. That was the
special case. The general rule is: **the store changes only through the
promoter.** Deposits arrive through it (`run`), records are rewritten by
it (`recategorise`), and from this revision datasets leave through it
(`remove`). The promoter holds the only key that can write a strand
prefix, every action it takes is a line on the audit trail, and the
derived layers (index, embeddings, passages) follow from what it did.
Nobody deletes objects under a strand prefix by hand.

## 2. The command

    python -m promoter remove --dataset ID [--dataset ID ...] [--reason TEXT] [--by NAME]
    python -m promoter remove --dataset ID --confirm ID            # the real thing
    python -m promoter remove --staging DEPOSIT_ID [--confirm DEPOSIT_ID]

- The plan is the default: without `--confirm` nothing is touched and
  the run says what it would do (`would_remove` per dataset with the
  record's uuid, creation date and depositors, the counts of objects,
  versions, delete markers and bytes, and how many other datasets name
  it in `derived_from`; then `remaining`, the identifiers that stay).
- `--confirm` must repeat every identifier exactly, one per `--dataset`;
  anything else refuses the whole run before any request (exit 2).
  There is no `--all`, no `--strand`, no wildcard.
- For each dataset, in order: the identifier must be well formed; the
  record `dataset.<name>.json` must be there, parse, and carry an
  `identifier` equal to its location (the typo guard: a prefix with
  members but no record, or a record that disagrees with where it sits,
  is refused and the run goes on with the others); the current
  `PROMOTER_AUTHORISED` policy must let `--by` deposit to that strand.
  Then every version under `<identifier>/` (the trailing slash is what
  keeps `.../test` away from `.../test-dataset`) is listed and deleted,
  members first in batches, the record last in a call of its own after
  a fresh listing shows nothing else left. A run that dies between the
  two leaves a prefix whose record still says it is a dataset, so the
  retry recognises it. Anything still there afterwards is
  `remove_incomplete` and exit 1.
- The audit line `dataset_removed` carries the identifier, uuid,
  counts, the record's `created` and `depositors`, `--reason` and
  `--by`. The trail is never pruned: a removal is an entry, not a gap.
- After the datasets: the index and embeddings are rewritten exactly as
  after a `run` that changed a record, and each removed dataset's
  passages file is deleted (`passages_removed`, "no record in place").
- `--staging DEPOSIT_ID` is the same shape for an abandoned deposit
  under the configured staging prefix: found by id under any user,
  refused when its record was written or its control object says
  complete ("promote or let it expire"), control object last, nothing
  derived to rebuild.
- Exit codes as every promoter command: 0 everything removed, 1 any
  identifier refused or incomplete, 2 configuration or `--confirm`
  mismatch or (a real run) the audit object not written.

## 3. What it never does

- Touch `index/`, `audit/` or any prefix it did not list.
- Rewrite another record. A dataset whose `derived_from` names the
  removed one keeps the reference with its uuid and version; the find
  page shows it as unresolved. The plan counts such referrers so the
  operator knows.
- Prune the audit trail or the journal.
- Accept a wildcard, or run for real without `--confirm`.

## 4. Deferred

The staging prefix move from `staging/_test` to `staging` (both VMs by
`.env`) and its documentation; a request form for stewards; retirement
of whole projects at the end of their life, which is this command run
per dataset with the governance handbook's reason until a case asks
for more.
