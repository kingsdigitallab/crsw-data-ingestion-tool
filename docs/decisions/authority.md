<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/authority.py, kept as a list -->

# Decisions behind `crsw_deposit/authority.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r9" is `DEPOSIT_TOOL_SPEC_R9.md`
and "r11 §3" section 3 of `_R11.md`.

## The module

- **Decision:** the vocabulary is a term authority file: every term ever approved, current or retired, with an append-only list of the changes that got it there.
  **Reason:** categories change after deposit, and a record's old terms have to be mapped forward by rule, not by memory.
  **Reference:** r9.
- **Decision:** pure functions with no I/O.
  **Reason:** the vocabulary repository's CI, the command line, the promoter and the deposit tool all call the same code.
  **Reference:** r9.
- **Decision:** a file with no `terms` key (the shape from before r9) is left alone by everything except `facets_of`; a file with no `broader` parents behaves exactly as if the hierarchy did not exist.
  **Reason:** an old cached or bundled file must keep working.
  **Reference:** r9.
- **Decision:** a term's description (`scope`, a SKOS scope note, and `aliases`) is not identity.
  **Reason:** it does not change which records a term applies to at all. So `describe_term` edits it without a change entry, the version stays, and the promoter never acts on it. The vocabulary repository's history is the audit trail for such edits.
  **Reference:** `docs/web-deposit/vocabulary-hierarchy-and-scope-notes-handoff.md`.

## Reading

- **Decision:** `aliases_of` leaves out a retired term's aliases.
  **Reason:** nothing should resolve to a retired term.
- **Decision:** `providers_of` reads the vocabulary's `providers` block.
  **Reference:** r11 §3.
- **Decision:** `facets_from_terms` produces the flat lists that `vocab.all_terms` and the subject listing read; `normalise` fills them in on a copy.
  **Reason:** readers that only know the flat shape keep working.
  **Reference:** r9.

## Searching (`expand_subject`)

- **Decision:** a search for a retired term matches its current successors, and never raises.
  **Reason:** records never carry a retired term, because the promoter rewrites them. Without this a search for one would look like an empty store.
  **Reference:** r9.

## Validation (`validate_authority`)

- **Decision:** one validator, with every rule, returning plain messages.
  **Reason:** it runs in the vocabulary repository's CI and in the tool before a fetched file is trusted.
  **Reference:** r9.
- **Decision:** `providers`, when present, is a list of entries with a unique slug and a name.
  **Reference:** r11 §3.

## Mapping subjects forward (`map_subjects`)

- **Decision:** mapping a list that is already current changes nothing and reports nothing.
  **Reason:** the promoter replays the changes over every record on every run, so mapping twice must be a no-op.
  **Reference:** r9.
- **Decision:** a retired term with no change entry is still handled, from its own `replaced_by`.
  **Reason:** a hand-maintained file may lack the entry, and the result must never carry a retired term. This is where "records never carry a retired term" is enforced: the promoter rewrites records with this function, and a comment at that line says so.
  **Reference:** r9.

## Writing (`dump_authority`)

- **Decision:** the file is written with one term, one change and one domain per line.
  **Reason:** a change then shows as a small diff in review.
