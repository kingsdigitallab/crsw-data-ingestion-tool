# CRSW Deposit Tool — revision spec (r3)

Path key element reorder. Small change, but it touches key construction, the sidecar, and three documents.

**Supersedes:** `DEPOSIT_TOOL_SPEC.md` §4 path structure. Everything else in r1/r2 stands.
**Origin:** agreed with eResearch sysadmin — sensitivity moves above state so it forms a contiguous prefix for access policies.

---

## 1. The change

```
was:  {strand}/{project}/{state}/{sensitivity}/{filename}
now:  {strand}/{project}/{sensitivity}/{state}/{filename}

was:  rs2/csac/2_final/green/csac-clean-2025.csv
now:  rs2/csac/green/2_final/csac-clean-2025.csv
```

Sidecar key unchanged in form: `{data_key}.meta.json`.

### Why

Sensitivity is an access-control boundary; state is a workflow stage. Putting sensitivity higher makes all amber data in a project a single contiguous prefix, so a policy can grant on `rs2/csac/amber/*` in one statement. Under the old order the same grant needed three patterns (`rs2/csac/0_raw/amber/*`, `.../1_interim/amber/*`, `.../2_final/amber/*`) — and would break silently if a new state were ever added.

---

## 2. Code changes

Should be confined to `keys.py` if the module boundary held. If path construction has leaked into `deposit.py` or `sidecar.py`, fix that while you're here — this is exactly the change the separation was for.

- Reorder the two elements in the path builder.
- Update any path *parsing* (key → metadata) to match. Easy to miss if it lives apart from the builder.
- Update the preview display, which shows the key before confirmation.
- Prompt order in `deposit.py`: ask sensitivity before state, so the prompts follow the path. Not required, but the mismatch is confusing.
- Check for hardcoded example keys in help text, docstrings, and error messages.

### Tests

- Path builder produces `rs2/csac/green/2_final/file.csv`.
- Round-trip: build a key from metadata, parse it back, get the same values.
- Sidecar `object_key` matches the actual upload target (a real risk if the two are built by separate code paths — they shouldn't be).
- `--dry-run` preview shows the new order.

---

## 3. Existing test data

Anything already deposited to `neil-test-01` under the old order is now wrong. Since it's all throwaway test material, delete rather than migrate:

```
rclone purge s3_kcl_neil:neil-test-01/rs1
rclone purge s3_kcl_neil:neil-test-01/rs2
```

Then re-deposit a couple of files to confirm the new keys land as expected.

**Worth doing before the Sima pilot, not after.** Once real data is deposited, a reorder means moving objects and rewriting every affected sidecar's `object_key`. Cheap now, tedious later.

---

## 4. Policy implications

The test policies from the earlier work grant on `arn:aws:s3:::*/rs2/*`, which is unaffected — strand is still the top element. But if any policy has been written to a deeper prefix, it needs updating.

The change makes sensitivity-scoped policies practical, which is new capability rather than just tidiness. Example — read-only on a project's green data, no access to its amber:

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject"],
  "Resource": "arn:aws:s3:::*/rs2/csac/green/*"
}
```

Worth raising with eResearch whether the permission matrix should now carry sensitivity as a dimension, or whether strand-and-project scoping remains the working grain. That's a governance question, not a tool one — but the tool now supports either answer.

---

## 5. Documentation to update

Adds to the debt already listed in r2 §7. Still worth batching into one pass after the Sima pilot rather than trickling:

| Document | What changes |
|---|---|
| Handbook §4.2 | Hierarchy table and worked path example |
| Handbook §4.7 | Worked example key |
| Handbook appendix | Path convention at a glance |
| Sidecar spec §4 | Worked example keys (data and sidecar) |
| Sidecar spec §3.1 | `object_key` description if it spells out the order |
| Planning recap §7, §8 | Path convention block and key-minting example |
| `DEPOSIT_TOOL_SPEC.md` §4 | Path rules |

Target remains Handbook v0.5 / Sidecar spec v0.3.

---

## 6. Open question to close first

Ask the sysadmin whether sensitivity should sit *above* project rather than below it:

```
rs2/amber/csac/*     one prefix covers all amber in the strand
rs2/csac/amber/*     one prefix per project
```

The first is better if policies will be written per strand; the second keeps a project's files together when browsing, which is what researchers do daily. His answer likely depends on whether access is expected to be granted per-strand or per-project at scale.

Both are one-line changes in `keys.py` **now**. Neither is, once real data exists.
