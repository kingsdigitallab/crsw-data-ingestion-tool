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
        for fields in (export_dcat.DATASET_FIELDS, export_dcat.FILE_FIELDS):
            for name, entry in fields.items():
                for term in [entry["term"]] + (
                        [entry["also"]] if "also" in entry else []):
                    prefix = term.split(":", 1)[0]
                    self.assertIn(prefix, namespaces,
                                 "%s: %s uses undeclared prefix %r"
                                 % (name, term, prefix))


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
        # Step 1 of r8: the reference list passes through as data; step 2
        # renders it per kind.
        self.assertEqual(self.out["dcterms:source"], [
            {"kind": "dataset", "identifier": "rs2/csac/amber/1_interim/csac-clean"}])
        self.assertEqual(self.out["prov:wasDerivedFrom"], self.out["dcterms:source"])

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
