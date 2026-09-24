# CRSW subject vocabulary

This repository holds the **term authority file** for the Centre for the
Study of Slavery in War: `vocab.json`. It is the single approved list of
subject terms, data domains and provenance activity kinds that every
CRSW dataset record is checked against, and the history of how that
list has changed.

The deposit tool, the web deposit service and the promoter fetch this
file at run time. A term that is not in it is refused at deposit. A term
that is renamed, merged, split or retired here is applied to every
dataset already in the store by the promoter, which records what it did
in each dataset's record. Nothing in the store is ever edited by hand.

## Who may change it

The stewards named in `.github/CODEOWNERS`. A change is a pull request
to `main`; the branch is protected so that a pull request needs one
approval from a steward other than its author before it can be merged,
and the validation check must pass. Nobody pushes to `main` directly.

## How to propose a change

Open an issue and pick the form for what you want:

| form | when |
|---|---|
| Add a term | a new subject term, optionally under a broader one |
| Rename a term | the same idea, a better slug or label; the old slug is retired and points to the new one |
| Merge terms | two or more terms that mean the same thing become one; the others are retired and point to it |
| Split a term | one term that has come to mean several things; it is retired and points to all its successors |
| Retire a term | a term nobody should use any more, with no successor |
| Move a term | a term goes under a different broader term, or to the top level |

Submitting the form opens a pull request with the change applied. Read
the pull request's diff: it should touch only the term(s) named, the
`changes` list and the version date. A steward approves and merges.
The next deposit anywhere uses the new file.

What each change does to datasets already in the store:

- **Add** and **move**: nothing. Existing datasets keep their terms.
- **Rename** and **merge**: every dataset carrying the old term gets the
  new one, with a history entry by "vocabulary".
- **Split**: every dataset carrying the old term gets **all** the
  successors and a history entry marked for review. The steward then
  removes the ones that do not apply, dataset by dataset, through the
  promoter's change file.
- **Retire**: the term is removed from every dataset carrying it. A
  dataset always keeps at least one term; if retirement would leave
  none, the promoter refuses that dataset and says so.

## The file, briefly

```json
{
  "vocabulary_version": "2026-10-01",
  "domains": [...],
  "activities": [...],
  "terms": [
    {"slug": "forced-labour", "facet": "practices", "label": "Forced labour",
     "status": "current", "since": "2026-07-23"},
    {"slug": "debt-bondage", "facet": "practices", "label": "Debt bondage",
     "status": "retired", "since": "2026-07-23", "until": "2026-10-01",
     "replaced_by": ["forced-labour"]}
  ],
  "changes": [
    {"date": "2026-10-01", "kind": "merge", "from": ["debt-bondage"],
     "to": ["forced-labour"], "by": "k1078591", "note": "issue #12"}
  ],
  "facets": {"practices": ["forced-labour", ...], ...}
}
```

`terms` is every term ever approved. `changes` is append-only: an entry
is never edited or removed. `facets` is derived from the current terms
and kept for older readers. A term may name a `broader` term in the same
facet; if none does, the vocabulary is flat, which is fine.

The rules are enforced by the deposit tool's validator, which runs on
every pull request here (`.github/workflows/validate.yml`) and inside
every tool before it trusts a fetched file. The full specification is
`docs/specs/DEPOSIT_TOOL_SPEC_R9.md` in
[crsw-data-ingestion-tool](https://github.com/kingsdigitallab/crsw-data-ingestion-tool).

## Editing from a laptop instead

```
pip install "git+https://github.com/kingsdigitallab/crsw-data-ingestion-tool@main"
crsw-vocab --file vocab.json validate
crsw-vocab --file vocab.json merge debt-bondage --into forced-labour --by k1078591 --note "issue #12"
git checkout -b change/merge-debt-bondage && git commit -am "merge debt-bondage into forced-labour" && git push -u origin HEAD
gh pr create --fill
```

`crsw-vocab --help` lists the other commands. The command validates
before writing and refuses anything the rules forbid.

## Repository settings that are not files

Set on 24 September 2026; listed so they can be checked or redone:

1. **Branch protection on `main`**: a pull request is required; 1
   approval; stale approvals dismissed; review from Code Owners
   required; the `validate` check required. Administrators are not
   exempt from the review rule by convention, though the setting allows
   it.
2. **Actions permissions**: "Allow GitHub Actions to create and approve
   pull requests" is on; the apply-issue workflow needs it.
3. **Repository variable `TOOL_REF`** (optional): the tag or branch of
   crsw-data-ingestion-tool the workflows install. Unset means `main`;
   pin to a release tag once one exists.
4. **Visibility: public.** The tools fetch the file anonymously at run
   time, so a merged change is live on the next deposit anywhere. If the
   repository is ever made private again, every tool silently falls
   back to the copy bundled in its release, and branch protection is
   lost on the free plan.
