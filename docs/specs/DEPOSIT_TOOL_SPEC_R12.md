# CRSW Deposit Tool — revision spec (r12): the Centre strand, the bulk route, the uploader clause

Status: **proposal, not agreed.** Built on 23 October 2026 for the
(CD)ISaW corpus (`docs/cdisaw-surfacing-plan.md`). Read alongside r6 §1
(the key layout) and r10 (the store changes only through the promoter).
Nothing here changes the record schema beyond the strand enum, the
object labels, or the members-first-record-last rule.

## 1. The `centre` strand

`keys.STRANDS` gains `centre`, so identifiers may read
`centre/<project>/<sensitivity>/<state>/<dataset>`. It holds
Centre-curated data that belongs to no single research strand: the
first is the harmonised (CD)ISaW corpus, `centre/cdisaw/green/2_final/<slug>`,
owned by KDL on the council's behalf.

- **Who writes it:** the bulk route (§2) only, through staging and the
  promoter like every other deposit. `PROMOTER_AUTHORISED`, when it is
  a map, names who may stage to it.
- **Who does not:** the deposit form and the CLI. `keys.DEPOSIT_STRANDS`
  (rs1–rs4) is what they offer; the form's metadata check refuses
  `centre`, and `deposit.py --strand centre` stops with a message naming
  the bulk route.
- **Who reads it:** everything that reads a strand. Find data's strand
  filter, the promoter's scan, `audit`, `datasets` and the index all use
  `keys.STRANDS`.
- `dataset.schema.json` carries `centre` in the strand enum and both
  identifier patterns.
- **The explorer link:** with `CRSW_CDISAW_URL` set, a dataset whose
  identifier starts `centre/cdisaw/` shows "Explore this data in
  (CD)ISaW" on its page and its Find data row. The link opens the
  explorer's scope rail on the whole corpus, the dataset's own years and
  that dataset, and runs it:
  `<url>/#screen=rail&spatial=corpus&from=<start>&to=<end>&edited=1&datasets=<dataset>`
  (a year or month is widened to whole days; `edited=1` keeps the dates
  over the explorer's default window). The `corpus` dataset opens the
  explorer's start page. Blank means no link. Download is unchanged.

## 2. The bulk route

`crsw_web/bulk.py`, boto3 only, so a harvest script can import it from a
sibling checkout. `stage_dataset(store, user, rec, files, now,
copy_from=None, dry_run=False)` takes a record already built and
validated with `crsw_deposit.record` and the local file for each
manifest path, and leaves in `staging/<user>/<deposit-id>/` exactly what
the web route leaves after finalise:

1. Refuse, writing nothing, if the identifier does not match the
   record's fields, `user` is not among its `depositors`, or the local
   paths or sizes differ from the manifest (a path the manifest names
   that is not a file on disk counts as missing).
2. Create the control object (open), put each member with the four
   labels (`upload_file`, which handles multipart), put the record last
   with its own labels, read it back, mark the control object complete.
3. `copy_from` (an existing prefix in the same bucket): a member found
   there at the same size is copied server-side with
   `MetadataDirective=REPLACE` and the new labels instead of uploaded.
   The promoter re-hashes every member against the manifest, so a
   stale copy is reported and left in staging, never promoted.
4. `dry_run`: no writes; the member list with what would be copied.
5. The control object is created before the first member. If anything
   fails after that (the network, a file gone), `StagingInterrupted`
   (a `BulkError`) carries the user and deposit id and names the
   `promoter remove --staging ID --confirm ID` that clears the open
   deposit; it is never promoted, but it counts against the depositor's
   staging until removed. A `KeyboardInterrupt` is re-raised as itself
   with the same `deposit_id` and `user` attributes.
6. Refuse, writing nothing, with `AlreadyStaged` when a complete deposit
   of the same identifier is already in staging (any user): it is
   waiting for the promoter, and staging it again would upload and
   promote it twice. An open deposit does not count; a dry run given no
   store client cannot look and skips the check.

`python -m crsw_web.bulk --record FILE --root DIR [--user K]
[--copy-from PREFIX] [--dry-run]` does the same for one dataset from the
shell, validating the record against the vocabulary first and reading
`CRSW_S3_*` and `CRSW_STAGING_PREFIX` as the service does.

The promoter needs no new command: a bulk deposit is a complete deposit.

A finished deposit the promoter refuses (a stale `copy_from` member that
fails the re-hash, a provider the vocabulary lacks) would otherwise sit
in staging, re-checked and refused on every run. `promoter remove
--staging ID --confirm ID` now removes a finished deposit when, and only
when, the promoter's checks refuse it for its own content: stored bytes
or sizes that are not the manifest's, a missing or mislabelled object, a
record that is absent, unreadable or disagrees with the control object,
or a record written while the control object never reached complete (an
upload that died between the two; nothing finishes it)
(`remove.CONTENT_REFUSALS`). The problems go on the trail as
`refused_because`. A finished deposit that passes is still refused:
promote it. One refused for anything else (a record invalid against the
vocabulary the promoter fetched, authorisation, a size cap, the
destination record) is refused too, with the problems: it would promote
once the configuration is right, so the remedy is that, not deleting
the upload.

## 3. The uploader may download

`access.may_download(user, sensitivity, strand, settings, depositors=())`
allows amber to anyone named in the record's `depositors`, before the
`CRSW_AMBER_ACCESS` rule. A person may always download what they
deposited. Red is still never served. The dataset page and the download
route pass the record's list; search passages and "Why this?" pass the
index row's (`depositors` is already an index column, so no rebuild).
With `CRSW_AMBER_ACCESS=off` the passage search still does not pull
amber rows at all, so a depositor's amber passages are not searched;
the dataset page and download are where the clause applies.
