# Review of r8 (provenance): the choices to make

23 September 2026. Comments on `DEPOSIT_TOOL_SPEC_R8.md` as a proposal. Nothing is built. Each item is a decision the spec should settle before anyone starts; options first, then a recommendation.

## 1. Where does the promoter write a corrected record?

**Question.** A web deposit cannot look up the identity of the dataset it was derived from, so the promoter fills that in later. Where does it write the corrected record?

- A. Only at the final destination. The copy left in staging is then different from the one that was moved.
- B. Correct the copy in staging first, then move it exactly as today. Staging and destination stay identical.

**Recommend B.** The rule "what was moved is what was checked" stays true, and the promoter's existing verification needs no exception. The correction is still written to the log.

*For the builder:* the promoter already has write access to staging; add the rewrite before the copy in `promote.py`.

## 2. What happens when an old-format record meets a new one?

**Question.** Existing records are format 0.5. The new format is 0.6. A deposit into an existing dataset will often pair one of each.

- A. Refuse, and ask someone to convert the old record by hand.
- B. Read both, write the result in the new format, and say so in the log.

**Recommend B.** Nobody should have to touch records by hand, and the conversion rule is mechanical.

*For the builder:* the strict version check in `record.parse_record` is shared by the CLI, the promoter and the web service, so the promoter and web tests must change in step 1, not only the record tests.

## 3. Who turns typed text into a structured reference?

**Question.** A researcher types "what this came from" as free text, in the CLI or in the web form. Something has to turn that into the new structured reference, and old records need the same conversion.

- A. Each route does it its own way.
- B. One shared rule, used by the CLI, the web form and the old-record conversion.

**Recommend B.** One rule cannot drift, and the project already requires one implementation per convention.

*For the builder:* a helper beside `upgrade_record` in `record.py`; the web form's existing free-text field in `crsw_web/metadata.py` calls it.

## 4. What about per-file lineage?

**Question.** A single file can also say what it was derived from. That field is a string today and nobody has used it.

- A. Structure it now, the same way as the dataset-level field.
- B. Leave it as a string until someone needs it.
- C. Remove it.

**Recommend B.** Doing A only half way, dataset structured and file not, would make the export render the same term two ways. B costs nothing now and keeps the door open.

*For the builder:* leave `export_dcat.py`'s file-level handling untouched.

## 5. What happens to provenance on a repeat deposit?

**Question.** A dataset is deposited again with new provenance information. What happens to the lists already in the record?

- A. New activities are added to the old ones. "Derived from" is replaced only if the depositor supplies a new one.
- B. Everything supplied replaces what was there.

**Recommend A**, which is what the spec says for the CLI. It must also be the rule for the web form, where the promoter does the merging, so the spec should state it once for both routes. Note that A means running the same pipeline twice records two activities, which is correct.

*For the builder:* the rule lives in `deposit_logic.assemble_record`, not in either caller.

## 6. Which formats does the schema accept?

**Question.** The published schema is the contract other projects build against.

- A. Only the new 0.6.
- B. Both 0.5 and 0.6.

**Recommend B**, and tell the cdisaw-parquet project which one it should write.

*For the builder:* `dataset.schema.json` changes from a single fixed version to a list of two.

## Small fixes to the text

- The example activity names a machine login as the person. It should be a KCL username, as everywhere else.
- "Warns on a branch name" cannot be checked. Say "must look like a commit hash" and stop there.
- Looking up another dataset to fill in a reference needs no special permission. Say so, because the promoter has a permission list and someone will ask.
- A reference to a parent that does not exist is a warning. Confirm the deposit still goes through with the reference left as typed.
- Fill in the parent's version number at the same time as its identity.
