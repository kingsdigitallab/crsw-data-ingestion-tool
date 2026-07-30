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

import record

MAPPING_PATH = Path(__file__).resolve().parent / "crsw-dc-mapping.json"
MAPPING = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))

DATASET_FIELDS = MAPPING["dataset_fields"]
FILE_FIELDS = MAPPING["file_fields"]


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


def dcat_dataset(rec: dict) -> dict:
    """The record as a dcat:Dataset (JSON-LD), terms from the mapping.
    Structured shapes (temporal, the license/rights branch, sensitivity
    value translation, combined identifiers, the provenance join) are
    keyed by field name; everything marked local renders under its
    mapped crsw:* term."""
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
        out[DATASET_FIELDS["derived_from"]["term"]] = rec["derived_from"]
        also = DATASET_FIELDS["derived_from"].get("also")
        if also:
            out[also] = rec["derived_from"]
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
