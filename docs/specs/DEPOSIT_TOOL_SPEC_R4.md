# CRSW Deposit Tool — revision spec (r4)

Two changes: the bucket name is now fixed, and project selection should offer what already exists.

**Supersedes:** nothing wholesale — refines r1 §4 (project rule) and the config behaviour in r2 §6.
**Builds on:** r3 path order (`{strand}/{project}/{sensitivity}/{state}/{filename}`).

---

## 1. Bucket default is now `crsw`

Confirmed with eResearch. The production bucket is `crsw`.

- Built-in default: `crsw`.
- `--bucket` override and the config-file value still take precedence (r2 §6 order: flag → config → default).
- Update the preflight "bucket not found" message, which currently names `crsw` as a placeholder — it's now the real default, so the message can drop the "if it looks right, contact eResearch" hedging and just say the bucket wasn't found on the remote.

That's the whole change. The interesting one follows.

---

## 2. Project selection offers existing prefixes

Right now a project name is typed free-hand, which is how `csac`, `CSAC` and `csac-data` become three sibling directories. The fix, already implied by the handbook ("offer existing names before allowing a new one") but not previously specified: after the strand is chosen, list what's already there and let the depositor pick or add.

### Behaviour

After strand selection, before anything else project-related:

```
Strand: rs2

Existing projects in rs2:
  [1] csac
  [2] trafficking-networks
  [3] peacekeeping-ops

  [n] new project
> 1
```

- List the immediate child prefixes of `{strand}/` in the bucket.
- Numbered select; `n` (or `new`) starts the add-a-project path.
- If the strand has no projects yet, skip straight to the new-project prompt with a note (`No projects in rs2 yet — creating the first.`).

### New project path

```
> n
New project name (lowercase, hyphens, no spaces): CSAC Data
  → normalised to: csac-data
  Create new project 'csac-data' in rs2? [y/N]
```

- Apply the same filename-safety normalisation already used elsewhere: lowercase, spaces → hyphens, strip anything outside `[a-z0-9-]`.
- Show the normalised form and require explicit confirmation — this is the guard against near-duplicates.
- **Warn on near-match:** if the normalised name is close to an existing one (e.g. differs only by a trailing `s`, a hyphen, or is a substring), say so and show the match before confirming. Doesn't have to be clever — a simple case-folded compare and a substring check catches the common cases. `csac` vs `csac-data` should prompt "similar to existing 'csac' — sure this is different?"

### How to list the prefixes

The prefixes live at `{strand}/` in the bucket. With the r3 path order, project is the element immediately below strand, so this is a shallow listing — do not walk the whole strand.

```
rclone lsf {remote}:{bucket}/{strand}/ --dirs-only
```

`lsf --dirs-only` returns immediate child "directories" (prefix segments) without recursing — cheap even for a large strand. Parse the lines, strip trailing slashes, that's the project list.

Notes:

- A scoped credential may be able to *write* `rs2/` but not *list* it, depending on the policy's `s3:prefix` condition. If the listing returns empty or errors on a strand the user can write to, fall back to the free-text new-project prompt rather than blocking — and don't present "no existing projects" as fact when it might be a permissions artefact. A one-line note ("couldn't list existing projects — you can still enter one") is honest; a confident empty list isn't.
- Cache the listing for the session; don't re-list if the tool loops over a batch.
- `--project` on the command line skips all of this (existing behaviour). If the supplied project doesn't exist in the strand, treat it as a new-project creation and confirm once, rather than failing.

### Interaction with batch deposits

Project is a batch-level field (shared across all files in a run), so this selection happens once per invocation, not per file. No change to batch semantics.

---

## 3. Documentation

The bucket name and the project-picker behaviour both want recording, but neither changes a *convention* — they're an operational default and a UI refinement. Lighter documentation touch than r2/r3:

| Document | Change |
|---|---|
| Handbook §4.6 | Note that project names are selected from existing prefixes where present; new ones need confirmation. One sentence — the mechanism is the tool's, not policy. |
| Handbook §5.1 (storage table) | Bucket name `crsw` where the endpoint is given |
| Planning recap §3 | Bucket name if storage specifics are stated |
| `DEPOSIT_TOOL_SPEC.md` §4, §7 | `crsw` default; project-picker behaviour |

These fold into the same batched revision as r2/r3 (Handbook v0.5, Sidecar spec v0.3) — still best held until after the Sima pilot. The doc updates accompanying *this* handoff (see separate note) cover the storage-decision changes that are worth landing now rather than batching.

---

## 4. Build order

1. Bucket default → `crsw`, adjust preflight message. Trivial; do first.
2. `lsf --dirs-only` project listing with the permissions-artefact fallback.
3. Numbered picker + new-project path with normalisation and confirmation.
4. Near-match warning.
5. Test: strand with projects, empty strand, scoped credential that can't list, `--project` for both existing and new names.
