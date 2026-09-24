import json
import tempfile
import unittest
from pathlib import Path

import export_dcat
from crsw_deposit import record
from tests.test_schema import worked_example, SCHEMA


class TestMappingCompleteness(unittest.TestCase):
    def test_worked_example_fully_mapped(self):
        # The r5 §9 / r6 §3.3 gap test: any record field the converter
        # cannot place is a mapping gap and fails here.
        self.assertEqual(export_dcat.unmapped_fields(worked_example()), [])

    def test_every_tier_field_is_placed(self):
        for field in (record.REQUIRED_FIELDS + record.RECOMMENDED_FIELDS
                      + record.OPTIONAL_FIELDS):
            self.assertIn(field, export_dcat.DATASET_FIELDS, field)

    def test_every_schema_property_is_in_the_mapping(self):
        # r6 §3.3: a schema field absent from the mapping is a build
        # error - this is that check for dataset_fields.
        for field in SCHEMA["properties"]:
            self.assertIn(field, export_dcat.DATASET_FIELDS, field)

    def test_every_schema_file_property_is_in_the_mapping(self):
        for field in SCHEMA["properties"]["files"]["items"]["properties"]:
            self.assertIn(field, export_dcat.FILE_FIELDS, field)

    def test_novel_field_reported(self):
        rec = worked_example()
        rec["surprise"] = "x"
        self.assertEqual(export_dcat.unmapped_fields(rec), ["surprise"])

    def test_novel_file_field_reported(self):
        rec = worked_example()
        rec["files"][0]["surprise"] = "x"
        self.assertEqual(export_dcat.unmapped_fields(rec), ["files[].surprise"])

    def test_mapping_schema_version_matches_record(self):
        self.assertEqual(export_dcat.MAPPING["schema_version"],
                         record.SCHEMA_VERSION)

    def test_every_mapped_prefix_is_declared(self):
        # A term whose prefix isn't in "namespaces" makes the exported
        # @context unresolvable JSON-LD - catches the spdx:checksum gap.
        namespaces = export_dcat.MAPPING["namespaces"]
        for fields in (export_dcat.DATASET_FIELDS, export_dcat.FILE_FIELDS,
                       export_dcat.REFERENCE_FIELDS,
                       export_dcat.ACTIVITY_FIELDS, export_dcat.TOOL_FIELDS):
            for name, entry in fields.items():
                if name.startswith("_"):
                    continue
                terms = [entry["term"]] + (
                    [entry["also"]] if "also" in entry else []) + (
                    [entry["type"]] if "type" in entry else [])
                for term in terms:
                    if term.startswith("@"):
                        continue  # JSON-LD keyword, not a prefixed term
                    prefix = term.split(":", 1)[0]
                    self.assertIn(prefix, namespaces,
                                 "%s: %s uses undeclared prefix %r"
                                 % (name, term, prefix))

    def test_reference_and_activity_fields_cover_the_validator(self):
        # r8 §5: every key validate_reference / validate_activity accepts
        # has a place in the export, or the PROV claim is not mechanical.
        self.assertEqual(set(k for k in export_dcat.REFERENCE_FIELDS
                             if not k.startswith("_")),
                         {"identifier", "dataset_uuid", "version",
                          "url", "citation", "retrieved"})
        self.assertEqual(set(k for k in export_dcat.ACTIVITY_FIELDS
                             if not k.startswith("_")),
                         {"activity", "description", "tool", "agent",
                          "inputs", "outputs", "started", "ended"})
        self.assertEqual(set(export_dcat.TOOL_FIELDS),
                         {"name", "repo", "commit", "version", "command",
                          "notebook"})


class TestDcatShape(unittest.TestCase):
    def setUp(self):
        self.rec = worked_example()
        self.out = export_dcat.dcat_dataset(self.rec)

    def test_core_terms(self):
        self.assertEqual(self.out["@type"], "dcat:Dataset")
        self.assertEqual(self.out["dcterms:identifier"],
                         [self.rec["dataset_uuid"], self.rec["identifier"]])
        self.assertEqual(self.out["dcterms:subject"],
                         ["armed-conflict", "forced-labour"])

    def test_sensitivity_value_translated(self):
        # The mapping's "values" table, not the raw green/amber code.
        self.assertEqual(self.out["dcterms:accessRights"], "public")
        rec = worked_example()
        rec["sensitivity"] = "amber"
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["dcterms:accessRights"], "restricted")

    def test_temporal_becomes_start_end_dates(self):
        self.assertEqual(self.out["dcterms:temporal"],
                         {"dcat:startDate": "1989",
                          "dcat:endDate": "2025-12-31"})

    def test_license_identifier_maps_to_license(self):
        self.assertEqual(self.out["dcterms:license"], "CC-BY-4.0")
        self.assertNotIn("dcterms:rights", self.out)

    def test_internal_only_maps_to_rights(self):
        rec = worked_example()
        rec["license"] = "internal-only"
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["dcterms:rights"], "internal-only")
        self.assertNotIn("dcterms:license", out)

    def test_provenance_is_not_source(self):
        # dcterms:source means derivation; acquisition narrative is
        # provenance (r6 §3.2) - here both exist and must not blur.
        self.assertIn("archive", self.out["dcterms:provenance"])
        # r8 §5: a dataset reference renders as the parent's identifier
        # node, under both dcterms:source and prov:wasDerivedFrom.
        self.assertEqual(self.out["dcterms:source"], [
            {"@type": "dcat:Dataset",
             "dcterms:identifier": ["rs2/csac/amber/1_interim/csac-clean"]}])
        self.assertEqual(self.out["prov:wasDerivedFrom"], self.out["dcterms:source"])
        self.assertNotIn("prov:wasGeneratedBy", self.out)

    def test_locals_stay_in_crsw_namespace(self):
        self.assertEqual(self.out["crsw:strand"], "rs2")
        self.assertEqual(self.out["crsw:dataset"], "csac-clean")
        self.assertEqual(self.out["crsw:version"], "3-0")
        self.assertNotIn("strand", self.out)
        self.assertNotIn("dataset", self.out)

    def test_distributions(self):
        dists = self.out["dcat:distribution"]
        self.assertEqual(len(dists), 2)
        self.assertEqual(dists[0]["dcat:downloadURL"], "csac-clean-2025.csv")
        self.assertEqual(dists[0]["dcat:byteSize"], 48211023)
        # crsw:*, not spdx:* - "spdx" is never declared in @context, so
        # a spdx:-prefixed term would make the JSON-LD unresolvable.
        self.assertEqual(len(dists[0]["crsw:checksumSha256"]), 64)
        self.assertEqual(dists[1]["dcterms:temporal"],
                         {"dcat:startDate": "1989-01-01",
                          "dcat:endDate": "1989-12-31"})

    def test_output_is_json_serialisable(self):
        json.dumps(self.out)


FIXTURES = Path(__file__).resolve().parent / "data" / "records"

# The r8 §2 worked example, verbatim.
CDB90_ACTIVITY = {
    "activity": "harmonise",
    "description": "(CD)ISaW ingest script for cdb90 run unchanged against "
                   "the Parquet shim instead of PostGIS; 18-column "
                   "crws-nodes projection",
    "tool": {"name": "cdisaw-parquet",
             "repo": "https://github.com/kingsdigitallab/cdisaw-parquet",
             "commit": "3f2a9c1e",
             "command": "cdisaw-parquet sweep --source cdb90 && "
                        "cdisaw-parquet project"},
    "inputs": [{"kind": "external", "url": "https://github.com/jrnold/CDB90"}],
    "outputs": ["wide/events/cdb90.parquet", "crws/events/cdb90.parquet"],
    "agent": "k1078591",
    "started": "2026-09-17T08:40:00Z",
    "ended": "2026-09-17T08:59:10Z",
}


class TestProvenanceExport(unittest.TestCase):
    """r8 step 2: derived_from references and provenance activities
    render as DCAT + PROV nodes, terms from the mapping."""

    def test_dataset_reference_lists_uuid_before_identifier(self):
        rec = worked_example()
        rec["derived_from"] = [{
            "kind": "dataset",
            "identifier": "rs2/csac/amber/1_interim/csac-clean",
            "dataset_uuid": "0f8b6f1e-6a3e-4a6c-9b1d-2e7c3d4f5a6b",
            "version": "2-1"}]
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["dcterms:source"], [{
            "@type": "dcat:Dataset",
            "dcterms:identifier": ["0f8b6f1e-6a3e-4a6c-9b1d-2e7c3d4f5a6b",
                                   "rs2/csac/amber/1_interim/csac-clean"],
            "crsw:version": "2-1"}])

    def test_external_reference_by_url_and_by_citation(self):
        rec = worked_example()
        rec["derived_from"] = [
            {"kind": "external", "url": "https://github.com/jrnold/CDB90",
             "retrieved": "2026-09-17"},
            {"kind": "external",
             "citation": "Arnold, J. (2014). CDB90, reformatted."}]
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["dcterms:source"], [
            {"@id": "https://github.com/jrnold/CDB90",
             "crsw:retrieved": "2026-09-17"},
            {"dcterms:bibliographicCitation":
             "Arnold, J. (2014). CDB90, reformatted."}])
        self.assertEqual(out["prov:wasDerivedFrom"], out["dcterms:source"])

    def test_activity_graph(self):
        rec = worked_example()
        rec["files"] = [
            record.manifest_entry("wide/events/cdb90.parquet", "ab" * 32, 1),
            record.manifest_entry("crws/events/cdb90.parquet", "cd" * 32, 1)]
        rec["provenance"] = [CDB90_ACTIVITY]
        errors, _ = record.validate_record(rec, {"armed-conflict",
                                                 "forced-labour"})
        self.assertEqual(errors, [])
        out = export_dcat.dcat_dataset(rec)
        acts = out["prov:wasGeneratedBy"]
        self.assertEqual(len(acts), 1)
        act = acts[0]
        self.assertEqual(act["@type"], "prov:Activity")
        self.assertEqual(act["crsw:activityKind"], "harmonise")
        self.assertIn("Parquet shim", act["dcterms:description"])
        self.assertEqual(act["prov:wasAssociatedWith"], [
            {"@type": "prov:SoftwareAgent",
             "dcterms:title": "cdisaw-parquet",
             "crsw:repo": "https://github.com/kingsdigitallab/cdisaw-parquet",
             "crsw:commit": "3f2a9c1e",
             "crsw:command": "cdisaw-parquet sweep --source cdb90 && "
                             "cdisaw-parquet project"},
            {"@type": "prov:Person", "dcterms:identifier": "k1078591"}])
        self.assertEqual(act["prov:used"],
                         [{"@id": "https://github.com/jrnold/CDB90"}])
        self.assertEqual(act["prov:generated"], [
            {"@type": "dcat:Distribution",
             "dcat:downloadURL": "wide/events/cdb90.parquet"},
            {"@type": "dcat:Distribution",
             "dcat:downloadURL": "crws/events/cdb90.parquet"}])
        self.assertEqual(act["prov:startedAtTime"], "2026-09-17T08:40:00Z")
        self.assertEqual(act["prov:endedAtTime"], "2026-09-17T08:59:10Z")
        # "activity" is local, so it must not leak in as a bare key.
        self.assertNotIn("activity", act)
        json.dumps(out)

    def test_bare_path_input_renders_as_member(self):
        rec = worked_example()
        rec["provenance"] = [{"activity": "subset",
                              "inputs": ["csac-clean-2025.csv"],
                              "outputs": ["csac-annual-1989.csv"]}]
        out = export_dcat.dcat_dataset(rec)
        act = out["prov:wasGeneratedBy"][0]
        self.assertEqual(act["prov:used"], [
            {"@type": "dcat:Distribution",
             "dcat:downloadURL": "csac-clean-2025.csv"}])
        self.assertNotIn("prov:wasAssociatedWith", act)

    def test_manual_activity_is_just_a_kind(self):
        rec = worked_example()
        rec["provenance"] = [{"activity": "manual"}]
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["prov:wasGeneratedBy"],
                         [{"@type": "prov:Activity",
                           "crsw:activityKind": "manual"}])

    def test_cdb90_fixture_exports_as_dcat_and_prov(self):
        # The step 2 checkpoint: the real 0.5 cdb90 record, upgraded on
        # read, with the r8 §2 activity added, exports with its string
        # derived_from rendered as an external source and the activity
        # as a PROV graph. Round-trips through the command line too.
        text = (FIXTURES / "dataset.cdb90.json").read_text(encoding="utf-8")
        rec, upgraded = record.parse_record_with_status(text)
        self.assertTrue(upgraded)
        rec["provenance"] = [CDB90_ACTIVITY]
        self.assertEqual(export_dcat.unmapped_fields(rec), [])
        out = export_dcat.dcat_dataset(rec)
        self.assertEqual(out["dcterms:source"],
                         [{"@id": "https://github.com/jrnold/CDB90"}])
        self.assertEqual(out["prov:wasGeneratedBy"][0]["prov:used"],
                         out["dcterms:source"])
        self.assertEqual(out["crsw:schemaVersion"], "0.6")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "dataset.cdb90.json"
            src.write_text(record.record_json(rec), encoding="utf-8")
            dst = Path(d) / "out.json"
            self.assertEqual(export_dcat.main([str(src), "-o", str(dst)]), 0)
            again = json.loads(dst.read_text(encoding="utf-8"))
        self.assertEqual(again, out)

    def test_untouched_05_fixture_still_exports(self):
        # No provenance at all: the upgraded string reference renders,
        # nothing is invented (r8 §6).
        text = (FIXTURES / "dataset.icews-conflict.json").read_text(
            encoding="utf-8")
        out = export_dcat.dcat_dataset(record.parse_record(text))
        self.assertNotIn("prov:wasGeneratedBy", out)
        self.assertEqual(len(out["dcterms:source"]), 1)


class TestCli(unittest.TestCase):
    def test_round_trip_via_files(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "dataset.csac-clean.json"
            src.write_text(record.record_json(worked_example()),
                           encoding="utf-8")
            dst = Path(d) / "out.json"
            code = export_dcat.main([str(src), "-o", str(dst)])
            self.assertEqual(code, 0)
            out = json.loads(dst.read_text(encoding="utf-8"))
        self.assertEqual(out["@type"], "dcat:Dataset")

    def test_unusable_record_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "dataset.csac-clean.json"
            src.write_text("{not json", encoding="utf-8")
            code = export_dcat.main([str(src)])
        self.assertNotEqual(code, 0)

    def test_unmapped_field_is_build_error(self):
        with tempfile.TemporaryDirectory() as d:
            rec = worked_example()
            rec["surprise"] = "x"
            src = Path(d) / "dataset.csac-clean.json"
            src.write_text(record.record_json(rec), encoding="utf-8")
            code = export_dcat.main([str(src)])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
