"""DCAT export for a CRSW dataset record (r5 §9).

Reads a dataset.meta.json and emits a DCAT dataset description as
JSON-LD, using the r5 §3 mapping table. Stdlib only, no role in the
deposit flow - its job is to prove that catalogue ingest is a
mechanical conversion, and to keep the mapping honest: any record
field the converter cannot place is a mapping gap (unmapped_fields).

Usage: python export_dcat.py path/to/dataset.meta.json [-o out.json]
"""
import argparse
import json
import sys
from typing import List

import record

# Local namespace IRI - a placeholder until the Centre owns a real one;
# the prefix is what matters for the mapping's shape.
CRSW_NS = "urn:x-crsw:terms#"

CONTEXT = {
    "dcterms": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
    "crsw": CRSW_NS,
}

# Fields that are deliberately local (r5 §3): they export under the
# crsw: prefix rather than pretending to be Dublin Core.
LOCAL_FIELDS = (
    "schema_version", "strand", "project", "state", "domain", "version",
    "vocabulary_version", "steward", "ethics_ref", "notes",
)

# Fields the converter consumes into standard terms. Everything in a
# record must appear here or in LOCAL_FIELDS - see unmapped_fields.
_MAPPED_FIELDS = (
    "dataset_uuid", "identifier", "abstract", "subjects", "sensitivity",
    "coverage_start", "coverage_end", "created", "modified", "licence",
    "creator", "depositors", "source_type", "source_detail",
    "derived_from", "language", "spatial", "files",
)

_ENTRY_LOCAL = ("checksum_sha256", "coverage_start", "coverage_end",
                "derived_from", "notes")


def unmapped_fields(rec: dict) -> List[str]:
    """Record fields the mapping does not place - must be empty; a new
    field lands here until §3 says where it goes (the r5 §9 gap test)."""
    known = set(_MAPPED_FIELDS) | set(LOCAL_FIELDS)
    return sorted(k for k in rec if k not in known)


def _distribution(entry: dict) -> dict:
    dist = {
        "@type": "dcat:Distribution",
        "dcterms:title": entry["path"],
        "dcat:byteSize": entry["bytes"],
        "crsw:checksum_sha256": entry["checksum_sha256"],
    }
    if entry.get("format"):
        dist["dcterms:format"] = entry["format"]
    for field in ("coverage_start", "coverage_end", "derived_from", "notes"):
        if entry.get(field):
            dist["crsw:%s" % field] = entry[field]
    return dist


def dcat_dataset(rec: dict) -> dict:
    """The record as a dcat:Dataset (JSON-LD), per the r5 §3 table."""
    out = {
        "@context": dict(CONTEXT),
        "@type": "dcat:Dataset",
        "dcterms:identifier": [rec["dataset_uuid"], rec["identifier"]],
        "dcterms:description": rec["abstract"],
        "dcterms:subject": list(rec["subjects"]),
        "dcterms:accessRights": rec["sensitivity"],
        "dcterms:temporal": {"dcat:startDate": rec["coverage_start"],
                             "dcat:endDate": rec["coverage_end"]},
        "dcterms:dateSubmitted": rec["created"],
        "dcterms:modified": rec["modified"],
    }
    # Licence identifiers map to dcterms:license; amber's internal-only
    # is a rights statement, not a licence IRI (r5 §3).
    if rec.get("licence") == "internal-only":
        out["dcterms:rights"] = "internal-only"
    elif rec.get("licence"):
        out["dcterms:license"] = rec["licence"]
    if rec.get("creator"):
        out["dcterms:creator"] = rec["creator"]
    if rec.get("depositors"):
        out["dcterms:contributor"] = list(rec["depositors"])
    # Acquisition narrative is provenance. NOT dcterms:source, which
    # means derivation - do not blur them (r5 §3).
    if rec.get("source_type") or rec.get("source_detail"):
        out["dcterms:provenance"] = ": ".join(
            part for part in (rec.get("source_type"),
                              rec.get("source_detail")) if part)
    if rec.get("derived_from"):
        out["dcterms:source"] = rec["derived_from"]
    if rec.get("language"):
        out["dcterms:language"] = list(rec["language"])
    if rec.get("spatial"):
        out["dcterms:spatial"] = rec["spatial"]
    for field in LOCAL_FIELDS:
        if rec.get(field):
            out["crsw:%s" % field] = rec[field]
    out["dcat:distribution"] = [_distribution(e) for e in rec["files"]]
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_dcat.py",
        description="Convert a CRSW dataset record to a DCAT (JSON-LD) "
                    "dataset description.")
    parser.add_argument("record_file", help="path to a dataset.meta.json")
    parser.add_argument("-o", "--output", default=None,
                        help="write here instead of stdout")
    args = parser.parse_args(argv)

    try:
        text = open(args.record_file, encoding="utf-8").read()
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
        print("Warning: no mapping for record field(s): %s"
              % ", ".join(gaps), file=sys.stderr)

    output = json.dumps(dcat_dataset(rec), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
