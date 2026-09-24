"""DCAT export for a CRSW dataset record (r6 §3).

The Dublin Core mapping lives in crsw-dc-mapping.json — the single
source of truth, shipped alongside dataset.schema.json and governed by
the same schema_version. This converter is a thin renderer of it:
term strings come from the mapping, never from code. A record field
absent from the mapping is a BUILD ERROR (exit 2), not a warning —
that check is what keeps the mapping in step as fields are added.

Stdlib only, no role in the deposit flow.

Usage: python export_dcat.py path/to/dataset.<slug>.json [-o out.json]
"""
import argparse
import json
import sys
from pathlib import Path
from typing import List

from crsw_deposit import record

MAPPING_PATH = Path(__file__).resolve().parent / "crsw-dc-mapping.json"
MAPPING = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))

DATASET_FIELDS = MAPPING["dataset_fields"]
FILE_FIELDS = MAPPING["file_fields"]
REFERENCE_FIELDS = MAPPING["reference_fields"]
ACTIVITY_FIELDS = MAPPING["activity_fields"]
TOOL_FIELDS = MAPPING["tool_fields"]


def unmapped_fields(rec: dict) -> List[str]:
    """Record fields the mapping does not place — must be empty. A new
    field lands here until crsw-dc-mapping.json says where it goes."""
    gaps = sorted(k for k in rec if k not in DATASET_FIELDS)
    for entry in rec.get("files") or []:
        if isinstance(entry, dict):
            gaps.extend("files[].%s" % k for k in sorted(entry)
                        if k not in FILE_FIELDS)
    seen = set()
    return [g for g in gaps if not (g in seen or seen.add(g))]


def _temporal(t: dict) -> dict:
    # Per the mapping note: start/end become dcat:startDate/dcat:endDate.
    return {"dcat:startDate": t.get("start"), "dcat:endDate": t.get("end")}


def _distribution(entry: dict) -> dict:
    dist = {
        "@type": "dcat:Distribution",
        FILE_FIELDS["path"]["term"]: entry["path"],
        FILE_FIELDS["bytes"]["term"]: entry["bytes"],
        FILE_FIELDS["checksum_sha256"]["term"]: entry["checksum_sha256"],
    }
    if entry.get("format"):
        dist[FILE_FIELDS["format"]["term"]] = entry["format"]
    if entry.get("temporal"):
        dist[FILE_FIELDS["temporal"]["term"]] = _temporal(entry["temporal"])
    if entry.get("derived_from"):
        dist[FILE_FIELDS["derived_from"]["term"]] = entry["derived_from"]
        also = FILE_FIELDS["derived_from"].get("also")
        if also:
            dist[also] = entry["derived_from"]
    if entry.get("notes"):
        dist[FILE_FIELDS["notes"]["term"]] = entry["notes"]
    return dist


def _member(path: str) -> dict:
    """A manifest member named from inside an activity, as the same
    dcat:Distribution node _distribution builds for it."""
    return {"@type": "dcat:Distribution", FILE_FIELDS["path"]["term"]: path}


def _reference(ref: dict) -> dict:
    """A derived_from reference or activity input (r8 §1) as a node. A
    dataset reference is a dcat:Dataset carrying the parent's identifier,
    the UUID first when known; an external one is named by its URL, or
    carries its citation when there is no URL."""
    if ref.get("kind") == "dataset":
        ids = [ref[k] for k in ("dataset_uuid", "identifier") if ref.get(k)]
        node = {"@type": "dcat:Dataset",
                REFERENCE_FIELDS["identifier"]["term"]: ids}
        if ref.get("version"):
            node[REFERENCE_FIELDS["version"]["term"]] = ref["version"]
        return node
    node = {}
    if ref.get("url"):
        node[REFERENCE_FIELDS["url"]["term"]] = ref["url"]
    if ref.get("citation"):
        node[REFERENCE_FIELDS["citation"]["term"]] = ref["citation"]
    if ref.get("retrieved"):
        node[REFERENCE_FIELDS["retrieved"]["term"]] = ref["retrieved"]
    return node


def _activity(act: dict) -> dict:
    """One provenance entry as a prov:Activity (r8 §5). The tool and the
    agent both hang off prov:wasAssociatedWith, typed apart."""
    node = {"@type": "prov:Activity",
            ACTIVITY_FIELDS["activity"]["term"]: act["activity"]}
    if act.get("description"):
        node[ACTIVITY_FIELDS["description"]["term"]] = act["description"]
    associated = []
    tool = act.get("tool")
    if tool:
        agent_node = {"@type": ACTIVITY_FIELDS["tool"]["type"]}
        for field, spec in TOOL_FIELDS.items():
            if tool.get(field):
                agent_node[spec["term"]] = tool[field]
        associated.append(agent_node)
    if act.get("agent"):
        associated.append({"@type": ACTIVITY_FIELDS["agent"]["type"],
                           DATASET_FIELDS["identifier"]["term"]: act["agent"]})
    if associated:
        node[ACTIVITY_FIELDS["tool"]["term"]] = associated
    if act.get("inputs"):
        node[ACTIVITY_FIELDS["inputs"]["term"]] = [
            _member(item) if isinstance(item, str) else _reference(item)
            for item in act["inputs"]]
    if act.get("outputs"):
        node[ACTIVITY_FIELDS["outputs"]["term"]] = [
            _member(path) for path in act["outputs"]]
    for field in ("started", "ended"):
        if act.get(field):
            node[ACTIVITY_FIELDS[field]["term"]] = act[field]
    return node


def dcat_dataset(rec: dict) -> dict:
    """The record as a dcat:Dataset (JSON-LD), terms from the mapping.
    Structured shapes (temporal, the license/rights branch, sensitivity
    value translation, combined identifiers, the provenance join, the
    derived_from references and the PROV activity graph) are keyed by
    field name; everything marked local renders under its mapped
    crsw:* term."""
    out = {
        "@context": dict(MAPPING["namespaces"]),
        "@type": "dcat:Dataset",
        DATASET_FIELDS["identifier"]["term"]: [rec["dataset_uuid"],
                                               rec["identifier"]],
        DATASET_FIELDS["abstract"]["term"]: rec["abstract"],
        DATASET_FIELDS["subject"]["term"]: list(rec["subject"]),
        DATASET_FIELDS["sensitivity"]["term"]:
            DATASET_FIELDS["sensitivity"].get("values", {}).get(
                rec["sensitivity"], rec["sensitivity"]),
        DATASET_FIELDS["temporal"]["term"]: _temporal(rec["temporal"]),
        DATASET_FIELDS["created"]["term"]: rec["created"],
        DATASET_FIELDS["modified"]["term"]: rec["modified"],
    }
    # Licence identifiers map to dcterms:license; amber's internal-only
    # is a rights statement, not a licence IRI (mapping note).
    if rec.get("license") == "internal-only":
        out["dcterms:rights"] = "internal-only"
    elif rec.get("license"):
        out[DATASET_FIELDS["license"]["term"]] = rec["license"]
    if rec.get("creator"):
        out[DATASET_FIELDS["creator"]["term"]] = rec["creator"]
    if rec.get("depositors"):
        out[DATASET_FIELDS["depositors"]["term"]] = list(rec["depositors"])
    # Acquisition narrative is provenance. NOT dcterms:source, which
    # means derivation - do not blur them (r6 §3.2).
    if rec.get("source_type") or rec.get("source_detail"):
        out[DATASET_FIELDS["source_type"]["term"]] = ": ".join(
            part for part in (rec.get("source_type"),
                              rec.get("source_detail")) if part)
    if rec.get("derived_from"):
        sources = [_reference(ref) for ref in rec["derived_from"]]
        out[DATASET_FIELDS["derived_from"]["term"]] = sources
        also = DATASET_FIELDS["derived_from"].get("also")
        if also:
            out[also] = sources
    if rec.get("provenance"):
        out[DATASET_FIELDS["provenance"]["term"]] = [
            _activity(act) for act in rec["provenance"]]
    if rec.get("language"):
        out[DATASET_FIELDS["language"]["term"]] = list(rec["language"])
    if rec.get("spatial"):
        out[DATASET_FIELDS["spatial"]["term"]] = rec["spatial"]
    for name, spec in DATASET_FIELDS.items():
        if spec.get("local") and rec.get(name):
            out[spec["term"]] = rec[name]
    out[DATASET_FIELDS["files"]["term"]] = [
        _distribution(e) for e in rec["files"]]
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_dcat.py",
        description="Convert a CRSW dataset record to a DCAT (JSON-LD) "
                    "dataset description using crsw-dc-mapping.json.")
    parser.add_argument("record_file", help="path to a dataset.<slug>.json")
    parser.add_argument("-o", "--output", default=None,
                        help="write here instead of stdout")
    args = parser.parse_args(argv)

    try:
        with open(args.record_file, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print("Could not read %s: %s" % (args.record_file, e),
              file=sys.stderr)
        return 1
    try:
        rec = record.parse_record(text)
    except record.RecordParseError as e:
        print("Not a usable dataset record: %s" % e, file=sys.stderr)
        return 1

    gaps = unmapped_fields(rec)
    if gaps:
        print("Build error: crsw-dc-mapping.json has no entry for: %s\n"
              "Add the field to the mapping (with \"local\": true if it "
              "has no standard term)." % ", ".join(gaps), file=sys.stderr)
        return 2

    output = json.dumps(dcat_dataset(rec), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
