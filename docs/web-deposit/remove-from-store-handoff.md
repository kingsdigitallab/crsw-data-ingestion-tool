# Handoff: `promoter remove` — a dataset leaves the store in one step

16 October 2026. Motivation: six test datasets from August–September are
still in place, padding search results with copies of this repo's own spec
files, and removing them by hand is five careful steps across two machines
(delete every object version, rebuild the index and embeddings, sweep the
passages files, keep the audit honest, do the rebuild where the right
`.env` is). Each is a chance to get it wrong. The promoter is the only
thing that writes the strand prefixes, so it should be the only thing that
empties them. Principle, extending r9: **datasets leave the store only
through the promoter.**

## Command

    python -m promoter remove --dataset ID [--dataset ID ...] [--reason TEXT]
    python -m promoter remove --dataset ID --confirm ID          # the real thing
    python -m promoter remove --staging DEPOSIT_ID [--confirm DEPOSIT_ID]

Dry run is the default: no `--confirm` means print the plan and exit 0.
`--confirm` must repeat every `--dataset` identifier exactly (one `--confirm`
per `--dataset`); a mismatch refuses the whole run before touching
anything. There is no `--all`, no wildcard, no `--strand`. Runs on the
internal VM like `run`; same `.env.promoter`, same key.

## What one run does, in order, for each dataset

1. **Check it is a dataset.** `dataset.<name>.json` at the prefix parses
   (`record.parse_record`) and its `identifier` equals the prefix. A
   prefix with no record, or a record that disagrees with its location,
   is refused for that identifier ("not a dataset record in place") and
   the run continues with the others. This is what stops a typo removing
   a real dataset's files.
2. **List every object under the prefix, every version.**
   `list_object_versions` paginated: current versions, noncurrent
   versions and delete markers alike. Count and total bytes go in the
   plan. Nothing outside the prefix is ever listed.
3. **Delete them all** (`delete_objects` in batches of 1000, with
   `VersionId`), members first, record last — mirror of the write order,
   so a run that dies halfway leaves a prefix whose record is still there
   and step 1 still recognises it on retry. Then list again: anything
   left is reported as `remove_incomplete` and the run exits 1.
4. **Audit line** `dataset_removed`: identifier, dataset_uuid, objects,
   versions, bytes, reason, by (the env's user or the OS user), and the
   record's `created`/`depositors` so the trail still says who put it
   there. The trail is never pruned; the removal is a new entry, not an
   erasure.

After all datasets:

5. **Rebuild the derived layers**, exactly as a real `run` that changed a
   record does (`_index_after_run` or its equivalent): `index_written`
   with the remaining datasets, `embeddings_written` with their rows (rows
   of removed datasets gone), then the passages sweep (`passages_removed`
   per removed dataset, "no record in place"). Reuse the existing
   functions; do not reimplement.
6. **Finish line** with counts, `audit_written` as for every run. Exit 0
   if everything asked for was removed, 1 if any identifier was refused
   or incomplete.

`--staging DEPOSIT_ID`: the same shape for an abandoned deposit under
`staging/<prefix>/<user>/<id>/` — verify the id exists, delete every
version under it (control object last), `staging_removed` audit line, no
rebuild (nothing derived depends on staging). Only ids with no record
written are eligible; a complete deposit waiting for promotion is refused
("promote or let it expire").

## What it must never do

- Touch `index/`, `audit/`, or any prefix it did not list in step 2.
- Run without `--confirm` matching, or accept any form of wildcard.
- Remove a dataset the current `PROMOTER_AUTHORISED` policy would not
  have let the env's user deposit to (same check as `run`, in reverse).
- Prune the audit trail or the journal. A removal is a line, not a gap.
- Rewrite any other record. Datasets that name the removed one in
  `derived_from` keep the reference (with its uuid and version); the find
  page already shows a reference whose target is gone as unresolved. Note
  the count of such referrers in the plan so the operator knows.

## Dry-run output (the plan)

    would remove rs1/test/green/0_raw/datasettest
      record: dataset_uuid 481a1e7d-… created 2026-08-25 by neil_
      objects 9 (8 members + record), versions 11, delete markers 0, 162.1 KB
      referenced by derived_from: 0 datasets
    would remove rs2/kdl-test/green/0_raw/specifications
      …
    then: rebuild index and embeddings for 1 remaining dataset; sweep passages
    nothing done (no --confirm)

## Tests

- Refuses: no record; record whose identifier ≠ prefix; `--confirm`
  missing or mismatched; `--strand`/`--all` rejected by argparse.
- Removes every version including delete markers (moto or the existing
  fake S3 in tests, with versioning on); record deleted last; second
  listing empty.
- A failure after members are gone leaves the record; a retry succeeds.
- Audit line fields; `index_written` count drops; `passages_removed`
  per dataset; embeddings file loses the rows.
- `--staging` refuses a deposit with a record written.

## Docs

- `DEPOSIT_TOOL_SPEC_R10.md` (short): the principle and the command;
  marks r9's "categories change only through the promoter" as the
  general rule "the store changes only through the promoter".
- Runbook §5 gains "Removing a dataset": when (test data, a withdrawn
  deposit, retirement at the end of a project per the governance
  handbook), the dry-run-then-confirm habit, and that the audit trail is
  where the history lives afterwards.
- `CLAUDE.md` storage conventions: one line.

## First use

Dry run against the six test datasets, read the plan, then confirm. Then
`--staging` for the empty deposit from the 15 Oct first attempt (id in the
web VM's log; it is the one under `staging/_test/k1078591/` with no
record). Afterwards `/search.json?q=` should say 1 dataset, 5 files, 206
passages, and the find page should list one dataset. Record the counts in
§7 of the deposit sheet.

## Amendments after review (16 Oct, 15:50)

1. **List with a trailing slash.** Every S3 listing and every delete in
   step 2–3 uses `Prefix=identifier + "/"`, never the bare identifier.
   Without the slash, `…/0_raw/test` also matches `…/0_raw/test-dataset`
   or `…/0_raw/testingagain` if they ever sit at the same level. This is
   the one way the command could take a dataset nobody asked for. A test
   with two sibling datasets whose names share a prefix must show the
   second untouched.
2. **Shape before store.** `keys.dataset_prefix_ok(identifier)` first;
   a malformed identifier is refused before any request is made.
3. **Record last, across pages and batches.** The listing is paginated
   and deletes go in batches of 1000; sort so that every version of
   `dataset.<name>.json` is held back and deleted in its own final call
   after a re-list shows nothing else left under the prefix.
4. **The plan names what remains.** After the "would remove" entries,
   print "remaining after this run:" with the identifiers that stay
   (from `index.rows`), so the operator sees the keep-list, not only the
   kill-list. For the first use that line must read exactly
   `rs1/legal-sources/green/0_raw/international-instruments-sample`.
5. **Finding the orphaned staging deposit.** List
   `staging/_test/k1078591/`: two deposit ids; the one with no
   `dataset.*.json` beneath it is the 15 Oct first attempt. `--staging`
   refuses the other (its record was written, and it was promoted).

Existing behaviour the command reuses, confirmed in code: `index.rows`
reads records in place (removed ones simply aren't there);
`_refresh_embeddings` writes rows for present identifiers only and logs
the rest as `dropped` (`promoter/embed.py:188`); `promoter passages`
without scope sweeps files whose record is gone (`passages_removed`);
the web's DuckDB copy drops files missing from the listing on its next
sync (`crsw_web/passages.py:208–222`); `Catalogue` re-reads the index on
ETag change.

## Staging prefix (16 Oct, 16:00)

Both VMs move from `staging/_test` to `staging` by `.env` on 16 Oct (the
service key is scoped to `staging*`; the form is not in live use, so no
window). `--staging DEPOSIT_ID` therefore resolves under the configured
`PROMOTER_STAGING_PREFIX` only; it does not need to know about `_test`.
The one orphaned deposit under `staging/_test/k1078591/` from the 15 Oct
first attempt is removed by hand with the key, once, before the prefix
changes; after that `staging/_test/` is empty and is history. Update
`.env.example` (both), `CLAUDE.md` and the integration test's docstring
to describe `_test` as the laptop-era prefix rather than the default.

## Decisions at build (19 October 2026)

Built as `promoter/remove.py` + `cmd_remove` in `promoter/__main__.py`,
tests in `tests/test_promoter_remove.py` (moto with versioning on),
spec `docs/specs/DEPOSIT_TOOL_SPEC_R10.md`, runbook §5 "Removing a
dataset". Names above that the code does not have: the rebuild is
`_write_index` (no `_index_after_run`); there is no promoter S3
wrapper, the raw boto3 client is used; "by" is `--by` or the login (no
`PROMOTER_USER`); `_write_passages` cannot remove a file whose record
is gone, so `cmd_remove` calls `passages.remove` itself for each
removed dataset.

- **Dry-run output is JSON log lines**, like every other command, not
  the prose plan sketched above: `would_remove` per dataset with the
  same facts (uuid, created, depositors, objects, versions,
  delete_markers, bytes, referenced_by), one `remaining` line with the
  keep-list, `finish` with `nothing_done`. `promoter audit` renders them.
- **No `--dry-run` flag**: the plan is the default; `--confirm` is the
  only way to do it. A mismatch exits 2 before `start`.
- `remove_incomplete` (something left after the deletes) and
  `remove_failed` (a request failed) are distinct lines; both leave the
  record and exit 1; a retry finishes the job.
- **The staging prefix section (16 Oct, 16:00) is not acted on**: both
  VMs are still on `staging/_test` on 19 October. `--staging` resolves
  under the configured prefix, which today reaches the 15 Oct orphan,
  so it can be removed by the command rather than by hand. The move to
  `staging` and the example-env, CLAUDE.md and integration-test wording
  wait for it.
- One line in `promoter/log.py`: `Log.write` accepts a bare deposit id
  string, for the staging lines.

## First use, 19 October 2026 (v0.14.0 on the internal VM)

Dry run clean: six `would_remove` (test-dataset 3 objects/4 versions,
datasettest 10/10 with 1 marker, test 2/2, testingagain 3/3 with 1
marker, web-poc 3/3, specifications 11/12 with 3 markers; none
referenced), `remaining` exactly
`rs1/legal-sources/green/0_raw/international-instruments-sample`.

The confirmed run (11:27 UTC, run 20261019T112749894986Z-1cdb) failed
on every dataset at the first `delete_objects` call: `AccessDenied` on
each member version. The promoter key's grant, like the personal key's
in July 2026, covers read and write but not delete under the strand
prefixes (it deletes plain markers under `staging/`, which is how
promotion clears staging). Behaviour as designed: `remove_failed` per
dataset, every record untouched, no index rewrite, exit 1, the run on
the trail. Asked eResearch for `s3:DeleteObject` and
`s3:DeleteObjectVersion` on `crsw/rs1/*` … `rs4/*` (and the versioned
delete on `staging/*`) for the promoter's key; the same line is the
retry.

The same afternoon, with the grant pending, the six test datasets and
both `staging/_test` deposits were deleted by hand through the rclone
mount with the k1078591 key: plain deletes, so a marker on every key
and every version still in the bucket (38 entries under rs1/, 62 under
rs2/, 51 under staging/_test/). The store looks clean to every reader
once the derived layers are rebuilt (`$P index --embed`, `$P passages`
on the internal VM; counts below). So that the proper purge can follow,
`inspect` now reads the newest version of a record hidden under a
delete marker and says `record_hidden: true`; a prefix that never had a
record is still refused. Both VMs move to `PROMOTER_STAGING_PREFIX` /
`CRSW_STAGING_PREFIX=staging` the same day (the section above, acted on
now); the example env files, CLAUDE.md and the integration test's
docstring call `staging/_test` the laptop-era prefix. When the grant
lands, the six-identifier `remove` line is the retry and writes the
`dataset_removed` lines the trail is missing.

Counts after the rebuild on the internal VM (19 October, afternoon):
`index_written` datasets 1; `embeddings_written` embedded 0, kept 1,
dropped 6; `passages_written` for the sample 512 passages (5 files, all
kept, nothing re-embedded); four `passages_removed` (test-dataset,
datasettest, testingagain, specifications; the other two never had a
file); `passages_finished` failed 0. 512 of the 748 passages of the
morning were the sample's; the 236 that went were this repo's spec
files and the other test content. `datasets` lists one row.

The web VM, on the staging prefix and the v0.13.0 image, agrees:
`/search.json?q=` stats passages 512, datasets 1, files 5, current 512.
