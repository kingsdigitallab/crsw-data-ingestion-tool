import json
import unittest
from pathlib import Path

try:
    from crsw_web.metadata import validate_meta, default_license
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))

GOOD = {
    "strand": "rs2", "sensitivity": "green", "state": "0_raw",
    "project": "csac", "dataset": "poc-test", "domain": "quant",
    "version": "1-0", "coverage_start": "2020", "coverage_end": "2021",
    "subject": ["forced-labour"], "abstract": "words " * 60,
}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestValidateMeta(unittest.TestCase):
    def test_good_form_has_no_errors_and_fills_defaults(self):
        meta, errors, warnings = validate_meta(GOOD, VOCAB)
        self.assertEqual(errors, {})
        self.assertEqual(warnings, [])
        self.assertEqual(meta["license"], "CC-BY-4.0")
        self.assertEqual(meta["steward"], "Kevin Fahey")   # from domain
        self.assertEqual(meta["vocabulary_version"], VOCAB["vocabulary_version"])
        self.assertEqual(meta["subject"], ["forced-labour"])

    def test_red_is_refused_with_tre_message(self):
        _, errors, _ = validate_meta(dict(GOOD, sensitivity="red"), VOCAB)
        self.assertIn("TRE", errors["sensitivity"])

    def test_amber_defaults_to_internal_only(self):
        meta, errors, _ = validate_meta(dict(GOOD, sensitivity="amber"), VOCAB)
        self.assertEqual(errors, {})
        self.assertEqual(meta["license"], "internal-only")
        self.assertEqual(default_license("amber"), "internal-only")

    def test_slug_rules_name_the_normalised_form(self):
        _, errors, _ = validate_meta(dict(GOOD, project="My Project"), VOCAB)
        self.assertIn("'my-project'", errors["project"])

    def test_unknown_subject_and_empty_subject(self):
        _, errors, _ = validate_meta(dict(GOOD, subject=["zzz"]), VOCAB)
        self.assertIn("zzz", errors["subject"])
        _, errors, _ = validate_meta(dict(GOOD, subject=[]), VOCAB)
        self.assertIn("at least one", errors["subject"])

    def test_subject_as_comma_string(self):
        meta, errors, _ = validate_meta(
            dict(GOOD, subject="forced-labour, survey"), VOCAB)
        self.assertEqual(errors, {})
        self.assertEqual(meta["subject"], ["forced-labour", "survey"])

    def test_version_normalised_and_bad_version_rejected(self):
        meta, errors, _ = validate_meta(dict(GOOD, version="v2.1"), VOCAB)
        self.assertEqual(meta["version"], "2-1")
        _, errors, _ = validate_meta(dict(GOOD, version="3"), VOCAB)
        self.assertIn("version", errors)

    def test_coverage_and_domain_errors(self):
        _, errors, _ = validate_meta(
            dict(GOOD, coverage_start="20x", domain="nope"), VOCAB)
        self.assertIn("coverage_start", errors)
        self.assertIn("domain", errors)

    def test_short_abstract_warns_but_does_not_block(self):
        _, errors, warnings = validate_meta(dict(GOOD, abstract="tiny"), VOCAB)
        self.assertEqual(errors, {})
        self.assertTrue(any("Abstract is 1 words" in w for w in warnings))

    def test_source_type_enum(self):
        _, errors, _ = validate_meta(dict(GOOD, source_type="magic"), VOCAB)
        self.assertIn("source_type", errors)
        meta, errors, _ = validate_meta(dict(GOOD, source_type="archive"), VOCAB)
        self.assertEqual(meta["source_type"], "archive")

    def test_steward_from_form_wins_and_tbc_is_not_filled(self):
        meta, _, _ = validate_meta(dict(GOOD, steward="Someone"), VOCAB)
        self.assertEqual(meta["steward"], "Someone")
        meta, _, _ = validate_meta(dict(GOOD, domain="narr"), VOCAB)
        self.assertNotIn("steward", meta)
