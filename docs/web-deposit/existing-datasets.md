# Depositing to a dataset that already exists, through the web

Decision note, 1 October 2026. Shipped in v0.10.0.

## The question

A deposit to a prefix that already holds a dataset is merged by the promoter under the r5 rule (members added and updated, never removed). Through the command-line tool the researcher sees this coming: the tool reads the record, says "Existing dataset found … Adding to it", offers the record's values as defaults, and previews added, updated and unchanged files. Through the web form they saw nothing, because the service's staging key cannot read the destination. Worse, the form's defaults (version `1-0`, a fresh abstract, subjects, licence, creator) replaced the record's values at promotion, so a re-deposit to a dataset at version `3-0` wrote it back as `1-0`.

## Options

1. **Add to it, as the CLI does.** Keep the additive rule; make the form show, prefill and confirm. No change to the record, the promoter or the specs.
2. **Allow a true replace.** A deliberate option to remove members not in the new deposit. Reverses the additive-only decision (r5 Q4, r6 §8); needs a spec round and a new promoter action.
3. **Additive plus a "retire file" path.** A middle ground; also a spec change.

## Decision

Option 1. The read role, already on the web VM for the find page, is the source: the service reads the promoter's index for existence and the live record for prefill. Nothing is read with the staging key, and the staged record and the promoter's merge are unchanged, so promotion is exactly as before.

What a re-deposit inherits is now one rule in `crsw_deposit.deposit_logic.defaults_from_record`, used by the CLI's prompts and the form's "Use its details", and `version_not_lower` stops a silent version drop on both routes (the CLI warns and asks; the web refuses).

The form offers existing project and dataset names, says under the dataset name what will happen (exists, waiting for promotion, same name elsewhere, near an existing name, or new, with the r6 §8 restricted-access question for a new one), requires a tick to add to an existing dataset, labels each upload added, updated or unchanged, and says what the deposit did. The service enforces the same confirmation, so the page cannot be bypassed. With the read role off the form is as it was.

## Still out of scope

Member removal or a true replace; the promoter skipping identical members when it merges (it re-copies them; harmless); reading the destination with the staging key, ever.

## First live re-deposit, 19 October 2026

The KCL proxy's upload cap went from 4 MB to 100 MB that day, so the two
files of `rs1/legal-sources/green/0_raw/international-instruments-sample`
that were too big for the form on 15 October (CR2021_01026.pdf, 13.7 MB;
ContemporaryformsofSlavery_McDougall_03.pdf, 4.9 MB) were added through
it, as deposit a00eed9feb15, the first under the `staging` prefix. The
form found the dataset, prefilled from its record and asked to confirm.
The promoter's check said what it would do ("destination already has
dataset ... merging: dataset_uuid 66bed065 kept, 2 member(s) added, 0
updated"), copied the two files, wrote the record with 7 members and
`merged_with_existing: true`, kept the uuid and `created`, rewrote the
index and the dataset's embedding. The passages step then failed on the
platform's HTTP 429 (a 13.7 MB PDF is 128 embedding batches); v0.14.1
makes the client wait and ask again, and the re-run wrote 4,609 passages
(512 kept, 4,097 new; CR2021_01026.pdf alone is 1,076 pages). The second
file has no text layer and sits in the file as a `NO_TEXT` row: the OCR
question, still open.
