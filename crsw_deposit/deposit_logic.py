"""What a deposit means, whichever way the bytes move.

Which keys a batch lands at, how the dataset record is assembled from
the batch and any existing record, the exact bytes the record is
written as, and the check that every manifest entry is in the store.
The CLI (rclone) and the web service (boto3) both call these, so the
record and labels are byte-identical whichever route was used.

No user I/O, no network. Stdlib only. Time and identity are passed
in, never looked up."""
import json
from typing import Callable, Dict, List, Optional, Tuple

from . import keys
from . import record


def parse_provenance_file(text: str) -> Tuple[List[Dict], List[Dict]]:
    """Parse the text of a `--provenance FILE` and return (provenance,
    derived_from), each possibly empty. The file is a JSON list of
    activities, or an object with `provenance` and/or `derived_from`;
    strings in `derived_from` become references.

    Raises ValueError naming the entry for a shape error. The full
    activity and reference checks run later, in validate_record."""
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
# strand, project, sensitivity, state, dataset, domain (a list, primary
# first; a bare code is wrapped), version, abstract, subject (list),
# coverage_start, coverage_end. Optional: vocabulary_version, creator,
# source_type, source_detail, provider (list of slugs), license,
# steward, derived_from (list of references), provenance (list of
# activities), spatial (list of place codes), spatial_note, notes,
# language, ethics_ref.


def plan_keys(meta: Dict, members: List[str],
              display: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """Return (member, key) for every member, in input order. `display`,
    aligned with `members`, is what to call each one in an error message.

    Raises ValueError when two members would land at the same key, and
    whatever keys.build_key raises for an invalid part."""
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
    """Return the existing record's UUID, or a new one for a first deposit."""
    return existing["dataset_uuid"] if existing else mint()


# Descriptive fields a deposit to an existing dataset starts from.
# Not derived_from: assemble_record keeps the existing list unless this
# deposit supplies one, so a form must not echo it back.
INHERITED_FIELDS = ("domain", "steward", "version", "subject", "abstract", "license",
                    "source_type", "source_detail", "creator", "provider",
                    "spatial", "spatial_note", "notes")

# Descriptive fields a deposit keeps from the existing record unless it
# supplies its own, as derived_from is kept.
KEPT_UNLESS_SUPPLIED = ("provider", "spatial", "spatial_note", "notes",
                        "language", "ethics_ref")


def defaults_from_record(existing: Optional[Dict]) -> Dict:
    """Return the values a new deposit to an existing dataset begins with:
    the record's descriptive fields, and its coverage as coverage_start
    and coverage_end. Blank fields are left out."""
    if not existing:
        return {}
    out: Dict = {}
    for field in INHERITED_FIELDS:
        value = existing.get(field)
        if value:
            out[field] = list(value) if isinstance(value, list) else value
    temporal = existing.get("temporal") or {}
    for part, field in (("start", "coverage_start"), ("end", "coverage_end")):
        if temporal.get(part):
            out[field] = temporal[part]
    return out


def _version_tuple(value) -> Optional[Tuple[int, int]]:
    normalised = record.normalise_version(value) if isinstance(value, str) else None
    if normalised is None:
        return None
    major, minor = normalised.split("-")
    return int(major), int(minor)


def version_not_lower(new, existing) -> bool:
    """False if `new` is a lower version than `existing`. Anything that is
    not a version compares as fine; the validators say what is wrong with
    it."""
    a, b = _version_tuple(new), _version_tuple(existing)
    if a is None or b is None:
        return True
    return a >= b


def assemble_record(meta: Dict, existing: Optional[Dict],
                    entries: List[Dict], depositor: str, now: str,
                    dataset_uuid: str, created: Optional[str] = None
                    ) -> Tuple[Dict, List[Dict], List[str], List[str]]:
    """Return (record, union, added, updated): the record for this
    deposit, and the manifest changes for the caller's report.

    - The manifest is the union of existing and batch; nothing is removed.
    - Coverage widens over every member's own range and the old envelope.
    - `created` is the existing record's, else the `created` argument,
      else now; `modified` is now.
    - Depositors, provenance activities and category history accumulate.
    - derived_from, provider, spatial, spatial_note, notes, language and
      ethics_ref are this deposit's if it supplies them, else the existing
      record's: a re-deposit never silently drops them.

    The result is not validated here: callers run record.validate_record
    with their vocabulary and treat errors as fatal before writing."""
    existing = existing or {}
    existing_files = existing.get("files") or None
    provenance = list(existing.get("provenance") or []) + list(
        meta.get("provenance") or [])
    derived_from = meta.get("derived_from") or existing.get("derived_from")
    kept = {name: meta.get(name) or existing.get(name) for name in KEPT_UNLESS_SUPPLIED}
    # Category history accumulates the same way (the promoter adds
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
        provider=kept["provider"],
        license=meta.get("license"), steward=meta.get("steward"),
        depositors=record.append_depositor(
            existing.get("depositors"), depositor),
        derived_from=derived_from, provenance=provenance,
        category_history=category_history,
        language=kept["language"], ethics_ref=kept["ethics_ref"],
        spatial=kept["spatial"], spatial_note=kept["spatial_note"],
        notes=kept["notes"])
    return rec, union, added, updated


def record_bytes(rec: Dict) -> bytes:
    """Return the exact bytes a record is stored as: UTF-8, LF only,
    trailing newline."""
    # The record's checksum label is computed from these bytes, so they
    # must not vary by platform or by route.
    return record.record_json(rec).encode("utf-8")


def completion_problems(prefix: str, entries: List[Dict],
                        stored_size: Callable[[str], Optional[int]]
                        ) -> List[str]:
    """Return what is missing from the store or the wrong size there; empty
    when the deposit is complete. Every manifest entry must exist at
    prefix + '/' + path with the recorded size. `stored_size` maps an
    object key to its size in the store, or None when absent."""
    problems = []
    # Sizes only, never a checksum against an ETag.
    for entry in entries:
        key = prefix + "/" + entry["path"]
        size = stored_size(key)
        if size is None:
            problems.append("no object at %s" % key)
        elif size != entry["bytes"]:
            problems.append("size mismatch at %s: expected %d, stored %s"
                            % (key, entry["bytes"], size))
    return problems
