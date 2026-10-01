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
