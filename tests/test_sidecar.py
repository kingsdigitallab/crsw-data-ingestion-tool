import json
import tempfile
import unittest
from pathlib import Path

import sidecar

VOCAB_TERMS = {"armed-conflict", "forced-labour", "human-trafficking"}

GOOD_FIELDS = dict(
    object_key="rs2/csac/2_final/green/csac-clean-2025.csv",
    strand="rs2", domain="quant", project="csac",
    state="2_final", sensitivity="green",
    coverage_start="1989", coverage_end="2025-12-31",
    version="3-0",
    abstract=" ".join(["word"] * 150),
    subjects=["armed-conflict", "forced-labour"],
)


class TestChecksum(unittest.TestCase):
    def test_empty_file_matches_known_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.bin"
            p.write_bytes(b"")
            self.assertEqual(
                sidecar.sha256_file(p),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_chunked_reading_matches_whole_file(self):
        import hashlib
        data = b"x" * (3 * 1024 * 1024 + 17)  # crosses chunk boundaries
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.bin"
            p.write_bytes(data)
            self.assertEqual(sidecar.sha256_file(p, chunk_size=65536),
                             hashlib.sha256(data).hexdigest())


class TestFieldChecks(unittest.TestCase):
    def test_coverage_year_only_ok(self):
        self.assertIsNone(sidecar.coverage_error("1989"))

    def test_coverage_iso_date_ok(self):
        self.assertIsNone(sidecar.coverage_error("2025-12-31"))

    def test_coverage_garbage_rejected(self):
        for bad in ("31/12/2025", "2025-13-01", "89", "next year", ""):
            self.assertIsNotNone(sidecar.coverage_error(bad), bad)

    def test_abstract_in_range_no_warning(self):
        self.assertIsNone(sidecar.abstract_warning(" ".join(["w"] * 200)))

    def test_abstract_too_short_warns(self):
        self.assertIsNotNone(sidecar.abstract_warning("too short"))

    def test_abstract_too_long_warns(self):
        self.assertIsNotNone(sidecar.abstract_warning(" ".join(["w"] * 301)))

    def test_normalise_version_accepts_documented_forms(self):
        for raw in ("3-0", "v3-0", "3.0", "v3.0", "V3-0"):
            self.assertEqual(sidecar.normalise_version(raw), "3-0", raw)

    def test_normalise_version_rejects_bare_major(self):
        for raw in ("3", "v3", "", "3-0-1", "three-oh", "3_0"):
            self.assertIsNone(sidecar.normalise_version(raw), raw)

    def test_unknown_subjects_listed(self):
        self.assertEqual(
            sidecar.unknown_subjects(["armed-conflict", "dragons"], VOCAB_TERMS),
            ["dragons"])


class TestBuildAndValidate(unittest.TestCase):
    def test_build_includes_schema_version(self):
        sc = sidecar.build_sidecar(**GOOD_FIELDS)
        self.assertEqual(sc["schema_version"], "0.2")

    def test_build_omits_absent_optionals(self):
        sc = sidecar.build_sidecar(**GOOD_FIELDS)
        self.assertNotIn("derived_from", sc)
        self.assertNotIn("notes", sc)

    def test_build_keeps_field_order(self):
        sc = sidecar.build_sidecar(checksum_sha256="ab" * 32, **GOOD_FIELDS)
        keys_list = list(sc.keys())
        self.assertEqual(keys_list[0], "schema_version")
        self.assertLess(keys_list.index("object_key"), keys_list.index("subjects"))

    def test_validate_good_sidecar(self):
        sc = sidecar.build_sidecar(**GOOD_FIELDS)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertEqual(errors, [])

    def test_validate_missing_required_is_error(self):
        sc = sidecar.build_sidecar(**GOOD_FIELDS)
        del sc["abstract"]
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertTrue(any("abstract" in e for e in errors))

    def test_validate_unknown_subject_is_error(self):
        fields = dict(GOOD_FIELDS)
        fields["subjects"] = ["dragons"]
        sc = sidecar.build_sidecar(**fields)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertTrue(any("dragons" in e for e in errors))

    def test_validate_empty_subjects_is_error(self):
        fields = dict(GOOD_FIELDS)
        fields["subjects"] = []
        sc = sidecar.build_sidecar(**fields)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertTrue(any("subject" in e for e in errors))

    def test_validate_short_abstract_is_warning_not_error(self):
        fields = dict(GOOD_FIELDS)
        fields["abstract"] = "short"
        sc = sidecar.build_sidecar(**fields)
        errors, warnings = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertEqual(errors, [])
        self.assertTrue(any("abstract" in w for w in warnings))

    def test_unnormalisable_version_is_error(self):
        fields = dict(GOOD_FIELDS)
        fields["version"] = "3"
        sc = sidecar.build_sidecar(**fields)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertTrue(any("version" in e for e in errors))

    def test_non_canonical_version_is_warning(self):
        fields = dict(GOOD_FIELDS)
        fields["version"] = "v3-0"
        sc = sidecar.build_sidecar(**fields)
        errors, warnings = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertEqual(errors, [])
        self.assertTrue(any("version" in w for w in warnings))

    def test_json_round_trips(self):
        sc = sidecar.build_sidecar(**GOOD_FIELDS)
        text = sidecar.sidecar_json(sc)
        self.assertEqual(json.loads(text), sc)
        self.assertTrue(text.endswith("\n"))


class TestDomainValidation(unittest.TestCase):
    def test_fetched_codes_accepted(self):
        fields = dict(GOOD_FIELDS)
        fields["domain"] = "newdomain"
        sc = sidecar.build_sidecar(**fields)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS,
                                             domain_codes=["newdomain"])
        self.assertEqual(errors, [])

    def test_default_falls_back_to_builtin(self):
        fields = dict(GOOD_FIELDS)
        fields["domain"] = "newdomain"
        sc = sidecar.build_sidecar(**fields)
        errors, _ = sidecar.validate_sidecar(sc, VOCAB_TERMS)
        self.assertTrue(any("domain" in e for e in errors))


class TestAutoFields(unittest.TestCase):
    def test_utc_now_iso_shape(self):
        self.assertRegex(sidecar.utc_now_iso(),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_default_depositor_nonempty(self):
        self.assertTrue(sidecar.default_depositor())


if __name__ == "__main__":
    unittest.main()
