"""What a deposit means, independent of how bytes move.

The orchestration that was inside the CLI's perform_deposits: which
keys a batch lands at, how the dataset record is assembled from the
batch plus any existing record, the exact bytes the record is written
as, and the completion check that proves every manifest entry is in
the store. The CLI (rclone) and the web service (boto3) both call
these so the record and labels are byte-identical whichever route a
researcher used.

No user I/O, no network. Stdlib only. Time and identity are passed
in, never looked up, so tests are deterministic."""
import json
from typing import Callable, Dict, List, Optional, Tuple

from . import keys
from . import record


def parse_provenance_file(text: str) -> Tuple[List[Dict], List[Dict]]:
    """A `--provenance FILE` (r8 §3): either a JSON list of activities,
    or an object with `provenance` and/or `derived_from`. Returns
    (provenance, derived_from), each possibly empty. Shape errors raise
    ValueError naming the entry; the full activity and reference checks
    run in validate_record once the manifest exists. Strings in
    `derived_from` become references by the shared rule."""
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError("provenance file is not JSON: %s" % e)
    if isinstance(doc, list):
        doc = {"provenance": doc}
    if not isinstance(doc, dict):
        raise ValueError("provenance file must be a list of activities or "
                         "an object with provenance and/or derived_from")
    unknown = sorted(set(doc) - {"provenance", "derived_from"})
    if unknown:
        raise ValueError("provenance file has unknown key(s): %s"
                         % ", ".join(unknown))
    provenance = doc.get("provenance") or []
    if not isinstance(provenance, list):
        raise ValueError("provenance must be a list of activities")
    for i, act in enumerate(provenance):
        if not isinstance(act, dict) or not act.get("activity"):
            raise ValueError("provenance[%d] must be an object with an "
                             "activity" % (i + 1))
    derived = doc.get("derived_from") or []
    if isinstance(derived, str):
        derived = [derived]
    if not isinstance(derived, list):
        raise ValueError("derived_from must be a list")
    refs = []
    for i, item in enumerate(derived):
        if isinstance(item, str):
            refs.extend(record.references_from_lines([item]))
        elif isinstance(item, dict):
            refs.append(item)
        else:
            raise ValueError("derived_from[%d] must be a reference object "
                             "or a string" % (i + 1))
    return provenance, refs

# A "meta" dict is the validated interview/form result. Required keys:
# strand, project, sensitivity, state, dataset, domain, version,
# abstract, subject (list), coverage_start, coverage_end. Optional:
# vocabulary_version, creator, source_type, source_detail, license,
# steward, derived_from (list of references), provenance (list of
# activities).


def plan_keys(meta: Dict, members: List[str],
              display: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """(member, key) for every member, in input order. Raises ValueError
    when two members would land at the same key - that would be a
    silent overwrite inside one batch. build_key does the per-part
    validation (red, reserved names, member-path grammar).

    `display`, aligned with `members`, is what to call each one in the
    collision message (the CLI passes local paths; the web route can
    pass the browser's relative names)."""
    display = list(display) if display is not None else list(members)
    out = []
    seen = {}
    for member, name in zip(members, display):
        key = keys.build_key(meta["strand"], meta["project"],
                             meta["sensitivity"], meta["state"],
                             meta["dataset"], member)
        if key in seen:
            raise ValueError(
                "%s and %s would land at the same key (%s) - rename one "
                "and re-run" % (seen[key], name, key))
        seen[key] = name
        out.append((member, key))
    return out


def dataset_prefix(meta: Dict) -> str:
    return keys.dataset_prefix(meta["strand"], meta["project"],
                               meta["sensitivity"], meta["state"],
                               meta["dataset"])


def dataset_uuid_for(existing: Optional[Dict], mint=record.mint_uuid) -> str:
    """The existing record's UUID, or a fresh one for a first deposit
    (r5 Q1: minted once per prefix, stable across moves)."""
    return existing["dataset_uuid"] if existing else mint()


def assemble_record(meta: Dict, existing: Optional[Dict],
                    entries: List[Dict], depositor: str, now: str,
                    dataset_uuid: str, created: Optional[str] = None
                    ) -> Tuple[Dict, List[Dict], List[str], List[str]]:
    """The record for this deposit, plus (union, added, updated) for
    the caller's report.

    Rules carried over from the CLI unchanged (r5 Q4, Q5):
    - manifest is the union of existing and batch; nothing is removed
    - the coverage envelope widens over every member's own range AND the
      old envelope, so legacy members whose per-file coverage was never
      recorded stay contained
    - `created` is preserved from the existing record, else `created`
      if the caller knows when the dataset first came into being (the
      promoter passes the staged record's), else now; `modified` is now
    - depositors accumulate, first-seen order, no repeats
    - provenance activities accumulate too: the existing list followed by
      whatever this deposit supplies (r8 §3, decided); derived_from is
      this deposit's list if it supplies one, else the existing one
    The result is NOT validated here: callers run record.validate_record
    with their vocabulary and treat errors as fatal before writing."""
    existing = existing or {}
    existing_files = existing.get("files") or None
    provenance = list(existing.get("provenance") or []) + list(
        meta.get("provenance") or [])
    derived_from = meta.get("derived_from") or existing.get("derived_from")
    # r9: category history accumulates the same way (the promoter adds
    # entries when it maps a staged record's stale terms).
    category_history = list(existing.get("category_history") or []) + list(
        meta.get("category_history") or [])
    union, added, updated = record.merge_manifest(existing_files, entries)
    pairs = [record.temporal_pair(e.get("temporal")) for e in union]
    if existing:
        pairs.append(record.temporal_pair(existing.get("temporal")))
    cov_start, cov_end = record.widen(
        (meta["coverage_start"], meta["coverage_end"]), pairs)
    rec = record.build_record(
        dataset_uuid=dataset_uuid,
        identifier=dataset_prefix(meta),
        strand=meta["strand"], domain=meta["domain"],
        project=meta["project"], dataset=meta["dataset"],
        state=meta["state"], sensitivity=meta["sensitivity"],
        temporal=record.temporal_object(cov_start, cov_end),
        version=meta["version"], abstract=meta["abstract"],
        subject=list(meta["subject"]), files=union,
        created=existing.get("created") or created or now,
        modified=now,
        vocabulary_version=meta.get("vocabulary_version"),
        creator=meta.get("creator"),
        source_type=meta.get("source_type"),
        source_detail=meta.get("source_detail"),
        license=meta.get("license"), steward=meta.get("steward"),
        depositors=record.append_depositor(
            existing.get("depositors"), depositor),
        derived_from=derived_from, provenance=provenance,
        category_history=category_history)
    return rec, union, added, updated


def record_bytes(rec: Dict) -> bytes:
    """The exact bytes a record is stored as: UTF-8, LF only, trailing
    newline. The record's own checksum label is computed from these
    bytes, so they must not vary by platform (r7 §4) or by route."""
    return record.record_json(rec).encode("utf-8")


def completion_problems(prefix: str, entries: List[Dict],
                        stored_size: Callable[[str], Optional[int]]
                        ) -> List[str]:
    """Completion check (r5 Q4): every manifest entry exists at
    prefix + '/' + path with the recorded size. Sizes only, never
    checksum-vs-ETag (r2 §0). `stored_size` maps an object key to its
    size in the store, or None when absent. Returns problems, empty
    when the deposit is complete."""
    problems = []
    for entry in entries:
        key = prefix + "/" + entry["path"]
        size = stored_size(key)
        if size is None:
            problems.append("no object at %s" % key)
        elif size != entry["bytes"]:
            problems.append("size mismatch at %s: expected %d, stored %s"
                            % (key, entry["bytes"], size))
    return problems
