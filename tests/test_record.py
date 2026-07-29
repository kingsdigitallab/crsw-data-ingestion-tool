import json
import tempfile
import unittest
from pathlib import Path

import record

VOCAB_TERMS = {"armed-conflict", "forced-labour", "human-trafficking"}

class TestAutoFields(unittest.TestCase):
    def test_utc_now_iso_shape(self):
        self.assertRegex(record.utc_now_iso(),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_default_depositor_nonempty(self):
        self.assertTrue(record.default_depositor())


RECORD_FIELDS = dict(
    dataset_uuid="8f14e45f-ceea-467f-a34e-9db1c153f0a1",
    identifier="rs2/csac/green/2_final",
    strand="rs2", domain="quant", project="csac",
    state="2_final", sensitivity="green",
    coverage_start="1989", coverage_end="2025-12-31",
    version="3-0",
    abstract=" ".join(["word"] * 150),
    subjects=["armed-conflict", "forced-labour"],
    created="2026-07-29T10:15:00Z",
    modified="2026-07-29T10:15:00Z",
    files=[{"path": "csac-clean-2025.csv",
            "checksum_sha256": "ab" * 32,
            "bytes": 48211023}],
)


def _record(**overrides):
    fields = dict(RECORD_FIELDS)
    fields.update(overrides)
    return record.build_record(**fields)


class TestBuildRecord(unittest.TestCase):
    def test_schema_version_is_04(self):
        self.assertEqual(_record()["schema_version"], "0.4")

    def test_field_order_record_shape(self):
        rec = _record(vocabulary_version="2026-07-23", creator="CSAC team")
        order = list(rec.keys())
        self.assertEqual(order[0], "schema_version")
        self.assertEqual(order[-1], "files")
        self.assertLess(order.index("dataset_uuid"), order.index("strand"))
        self.assertLess(order.index("vocabulary_version"),
                        order.index("coverage_start"))
        self.assertLess(order.index("created"), order.index("modified"))

    def test_omits_absent_recommended_and_optional(self):
        rec = _record()
        for absent in ("creator", "depositors", "spatial", "derived_from",
                       "notes", "vocabulary_version"):
            self.assertNotIn(absent, rec)

    def test_depositors_list_kept(self):
        rec = _record(depositors=["njakeman", "kfahey"])
        self.assertEqual(rec["depositors"], ["njakeman", "kfahey"])


class TestValidateRecord(unittest.TestCase):
    def _errors(self, rec, **kwargs):
        errors, _ = record.validate_record(rec, VOCAB_TERMS, **kwargs)
        return errors

    def test_good_record_validates(self):
        self.assertEqual(self._errors(_record()), [])

    def test_missing_required_is_error(self):
        rec = _record()
        del rec["created"]
        self.assertTrue(any("created" in e for e in self._errors(rec)))

    def test_identifier_must_match_parts(self):
        rec = _record(identifier="rs2/csac/2_final/green")
        self.assertTrue(any("identifier" in e for e in self._errors(rec)))

    def test_bad_uuid_is_error(self):
        rec = _record(dataset_uuid="not-a-uuid")
        self.assertTrue(any("dataset_uuid" in e for e in self._errors(rec)))

    def test_reserved_manifest_path_is_error(self):
        rec = _record(files=[{"path": "dataset.meta.json",
                              "checksum_sha256": "ab" * 32, "bytes": 1}])
        self.assertTrue(any("reserved" in e for e in self._errors(rec)))

    def test_duplicate_manifest_paths_is_error(self):
        entry = {"path": "a.csv", "checksum_sha256": "ab" * 32, "bytes": 1}
        rec = _record(files=[entry, dict(entry)])
        self.assertTrue(any("duplicate" in e.lower() for e in self._errors(rec)))

    def test_bad_checksum_is_error(self):
        rec = _record(files=[{"path": "a.csv", "checksum_sha256": "zz",
                              "bytes": 1}])
        self.assertTrue(any("checksum" in e for e in self._errors(rec)))

    def test_bad_bytes_is_error(self):
        for bad in (-1, "12", True):
            rec = _record(files=[{"path": "a.csv",
                                  "checksum_sha256": "ab" * 32, "bytes": bad}])
            self.assertTrue(any("bytes" in e for e in self._errors(rec)),
                            repr(bad))

    def test_entry_coverage_outside_envelope_is_error(self):
        rec = _record(files=[{"path": "a.csv", "checksum_sha256": "ab" * 32,
                              "bytes": 1, "coverage_start": "1970-01-01",
                              "coverage_end": "1990"}])
        self.assertTrue(any("envelope" in e for e in self._errors(rec)))

    def test_entry_coverage_inside_envelope_ok(self):
        rec = _record(files=[{"path": "a.csv", "checksum_sha256": "ab" * 32,
                              "bytes": 1, "coverage_start": "1989-01-01",
                              "coverage_end": "1989-12-31"}])
        self.assertEqual(self._errors(rec), [])

    def test_depositors_must_be_list(self):
        rec = _record(depositors="njakeman")
        self.assertTrue(any("depositors" in e for e in self._errors(rec)))

    def test_unknown_subject_is_error(self):
        rec = _record(subjects=["dragons"])
        self.assertTrue(any("dragons" in e for e in self._errors(rec)))

    def test_fetched_domain_codes_used(self):
        rec = _record(domain="newdomain")
        self.assertEqual(self._errors(rec, domain_codes=["newdomain"]), [])
        self.assertTrue(any("domain" in e for e in self._errors(rec)))


class TestManifest(unittest.TestCase):
    def test_entry_shape_and_omitted_optionals(self):
        entry = record.manifest_entry("a.csv", "ab" * 32, 123)
        self.assertEqual(entry, {"path": "a.csv", "checksum_sha256": "ab" * 32,
                                 "bytes": 123})

    def test_entry_keeps_optionals(self):
        entry = record.manifest_entry("a.csv", "ab" * 32, 123,
                                      coverage_start="1989",
                                      coverage_end="1990", fmt="text/csv")
        self.assertEqual(entry["format"], "text/csv")
        self.assertEqual(list(entry)[:3], ["path", "checksum_sha256", "bytes"])

    def test_guess_format(self):
        self.assertEqual(record.guess_format("a.csv"), "text/csv")
        self.assertEqual(record.guess_format("b.pdf"), "application/pdf")
        self.assertIsNone(record.guess_format("weird.qgz2x"))

    def test_merge_adds_and_sorts(self):
        existing = [record.manifest_entry("b.csv", "ab" * 32, 1)]
        new = [record.manifest_entry("a.csv", "cd" * 32, 2)]
        union, added, changed = record.merge_manifest(existing, new)
        self.assertEqual([e["path"] for e in union], ["a.csv", "b.csv"])
        self.assertEqual(added, ["a.csv"])
        self.assertEqual(changed, [])

    def test_merge_replaces_same_path_and_reports_changed(self):
        existing = [record.manifest_entry("a.csv", "ab" * 32, 1)]
        new = [record.manifest_entry("a.csv", "cd" * 32, 2)]
        union, added, changed = record.merge_manifest(existing, new)
        self.assertEqual(len(union), 1)
        self.assertEqual(union[0]["checksum_sha256"], "cd" * 32)
        self.assertEqual(added, [])
        self.assertEqual(changed, ["a.csv"])

    def test_merge_is_additive_only(self):
        existing = [record.manifest_entry("keep.csv", "ab" * 32, 1)]
        union, _, _ = record.merge_manifest(existing, [])
        self.assertEqual([e["path"] for e in union], ["keep.csv"])

    def test_merge_from_no_existing_record(self):
        union, added, changed = record.merge_manifest(
            None, [record.manifest_entry("a.csv", "ab" * 32, 1)])
        self.assertEqual(added, ["a.csv"])

    def test_unchanged_requires_checksum_and_size(self):
        existing = [record.manifest_entry("a.csv", "ab" * 32, 1),
                    record.manifest_entry("b.csv", "ab" * 32, 1)]
        new = [record.manifest_entry("a.csv", "ab" * 32, 1),
               record.manifest_entry("b.csv", "cd" * 32, 1),
               record.manifest_entry("c.csv", "ab" * 32, 1)]
        self.assertEqual(record.unchanged_paths(existing, new), {"a.csv"})


class TestEnvelope(unittest.TestCase):
    def test_widen_extends_both_ends(self):
        start, end = record.widen(("1995", "2000"),
                                  [("1989-06-01", "2001-02-03")])
        self.assertEqual((start, end), ("1989-06-01", "2001-02-03"))

    def test_widen_keeps_year_form(self):
        start, end = record.widen(("1995-06-01", "2000"), [("1989", "1999")])
        self.assertEqual((start, end), ("1989", "2000"))

    def test_widen_year_expands_to_year_end(self):
        # 2000 as an end means 2000-12-31, so 2000-05-01 does not widen it.
        _, end = record.widen(("1995", "2000"), [("1996", "2000-05-01")])
        self.assertEqual(end, "2000")

    def test_widen_skips_empty_pairs(self):
        self.assertEqual(record.widen(("1995", "2000"), [(None, None)]),
                         ("1995", "2000"))

    def test_single_file_dataset_envelope_is_the_files(self):
        rec = _record(coverage_start="1989-01-01", coverage_end="1989-12-31",
                      files=[{"path": "a.csv", "checksum_sha256": "ab" * 32,
                              "bytes": 1, "coverage_start": "1989-01-01",
                              "coverage_end": "1989-12-31"}])
        self.assertEqual(record.envelope_errors(rec), [])

    def test_containment_violation_reported(self):
        rec = _record(files=[{"path": "a.csv", "checksum_sha256": "ab" * 32,
                              "bytes": 1, "coverage_end": "2026-01-01"}])
        self.assertTrue(record.envelope_errors(rec))


class TestParseRecord(unittest.TestCase):
    def test_round_trip(self):
        rec = _record()
        self.assertEqual(record.parse_record(record.record_json(rec)), rec)

    def test_invalid_json_raises(self):
        with self.assertRaises(record.RecordParseError):
            record.parse_record("{not json")

    def test_non_object_raises(self):
        with self.assertRaises(record.RecordParseError):
            record.parse_record("[1, 2]")

    def test_unknown_schema_version_raises(self):
        for version in ("0.3", "0.5", None):
            rec = _record()
            rec["schema_version"] = version
            with self.assertRaises(record.RecordParseError):
                record.parse_record(json.dumps(rec))

    def test_missing_or_empty_files_raises(self):
        rec = _record()
        rec["files"] = []
        with self.assertRaises(record.RecordParseError):
            record.parse_record(json.dumps(rec))


class TestDepositors(unittest.TestCase):
    def test_first_deposit(self):
        self.assertEqual(record.append_depositor(None, "njakeman"),
                         ["njakeman"])

    def test_no_duplicates_order_kept(self):
        self.assertEqual(
            record.append_depositor(["njakeman", "kfahey"], "njakeman"),
            ["njakeman", "kfahey"])

    def test_appends_new_name(self):
        self.assertEqual(record.append_depositor(["njakeman"], "kfahey"),
                         ["njakeman", "kfahey"])


class TestMintUuid(unittest.TestCase):
    def test_shape_and_uniqueness(self):
        a, b = record.mint_uuid(), record.mint_uuid()
        self.assertRegex(a, record._UUID4_RE)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
