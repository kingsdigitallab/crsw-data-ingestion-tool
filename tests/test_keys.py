import unittest

import keys


class TestBuildKey(unittest.TestCase):
    def test_happy_path(self):
        self.assertEqual(
            keys.build_key("rs2", "csac", "green", "2_final",
                          "sentinel2-imagery", "csac-clean-2025.csv"),
            "rs2/csac/green/2_final/sentinel2-imagery/csac-clean-2025.csv",
        )

    def test_key_uses_forward_slashes_only(self):
        key = keys.build_key("rs1", "treaty-texts", "amber", "0_raw",
                             "scans", "a.txt")
        self.assertNotIn("\\", key)
        self.assertEqual(key.count("/"), 5)

    def test_round_trip(self):
        # r3: element positions are part of the contract; a policy prefix
        # like rs2/csac/amber/* depends on them.
        meta = ("rs2", "csac", "amber", "1_interim", "labels", "b.csv")
        parts = keys.build_key(*meta).split("/")
        self.assertEqual(tuple(parts), meta)

    def test_invalid_strand_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs9", "csac", "green", "2_final", "ds", "a.csv")

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "csac", "green", "final", "ds", "a.csv")

    def test_red_raises_red_data_error(self):
        with self.assertRaises(keys.RedDataError):
            keys.build_key("rs2", "csac", "red", "2_final", "ds", "a.csv")

    def test_uppercase_project_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "CSAC", "green", "2_final", "ds", "a.csv")

    def test_invalid_dataset_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "csac", "green", "2_final", "Bad Slug",
                          "a.csv")

    def test_empty_filename_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "csac", "green", "2_final", "ds", "")

    def test_reserved_record_pattern_rejected(self):
        for name in ("dataset.meta.json", "dataset.foo.json",
                    "dataset..json"):
            with self.assertRaises(ValueError, msg=name):
                keys.build_key("rs2", "csac", "green", "2_final", "ds", name)

    def test_non_matching_names_allowed(self):
        for name in ("dataset.json", "mydataset.foo.json", "dataset.csv"):
            keys.build_key("rs2", "csac", "green", "2_final", "ds", name)


class TestReservedName(unittest.TestCase):
    def test_matches(self):
        for name in ("dataset.meta.json", "dataset.foo.json", "dataset..json"):
            self.assertTrue(keys.is_reserved_name(name), name)

    def test_non_matches(self):
        for name in ("dataset.json", "mydataset.foo.json", "", None):
            self.assertFalse(keys.is_reserved_name(name), repr(name))

    def test_record_filename(self):
        self.assertEqual(keys.record_filename("sentinel2-imagery"),
                         "dataset.sentinel2-imagery.json")


class TestRecordKey(unittest.TestCase):
    def test_dataset_prefix(self):
        self.assertEqual(
            keys.dataset_prefix("rs2", "csac", "green", "2_final", "ds"),
            "rs2/csac/green/2_final/ds")

    def test_prefix_is_the_identifier_shape(self):
        # r5 Q1: the prefix string IS the record's identifier.
        prefix = keys.dataset_prefix("rs1", "treaty-texts", "amber",
                                     "0_raw", "scans")
        self.assertEqual(tuple(prefix.split("/")),
                         ("rs1", "treaty-texts", "amber", "0_raw", "scans"))

    def test_record_key(self):
        self.assertEqual(
            keys.record_key("rs2", "csac", "green", "2_final",
                           "sentinel2-imagery"),
            "rs2/csac/green/2_final/sentinel2-imagery/"
            "dataset.sentinel2-imagery.json")

    def test_prefix_validates_parts(self):
        with self.assertRaises(ValueError):
            keys.dataset_prefix("rs9", "csac", "green", "2_final", "ds")
        with self.assertRaises(keys.RedDataError):
            keys.dataset_prefix("rs2", "csac", "red", "2_final", "ds")
        with self.assertRaises(ValueError):
            keys.dataset_prefix("rs2", "csac", "green", "2_final", "Bad Ds")


class TestProjectValidation(unittest.TestCase):
    def test_valid_projects(self):
        for name in ("csac", "csac-data", "a1", "x-y-z2"):
            self.assertTrue(keys.validate_project(name), name)

    def test_invalid_projects(self):
        for name in ("CSAC", "csac data", "csac_", "-csac", "csac-", "", "és"):
            self.assertFalse(keys.validate_project(name), repr(name))


class TestNormaliseProject(unittest.TestCase):
    def test_lowercase_and_spaces(self):
        self.assertEqual(keys.normalise_project("CSAC Data"), "csac-data")

    def test_strips_disallowed_chars(self):
        self.assertEqual(keys.normalise_project("Trafficking (2024)!"),
                         "trafficking-2024")

    def test_collapses_and_trims_hyphens(self):
        # spaces become hyphens, then runs collapse and edges trim
        self.assertEqual(keys.normalise_project("a - b"), "a-b")
        self.assertEqual(keys.normalise_project(" - csac - "), "csac")

    def test_disallowed_chars_strip_not_separate(self):
        # spec r4 §2: strip outside [a-z0-9-]; only spaces map to hyphens
        self.assertEqual(keys.normalise_project("a__b"), "ab")

    def test_unusable_input_returns_empty(self):
        self.assertEqual(keys.normalise_project("???"), "")
        self.assertEqual(keys.normalise_project(""), "")

    def test_nonempty_result_passes_validate_project(self):
        for raw in ("CSAC Data", "Trafficking (2024)!", "--a__b--",
                    "x", "és-café", "A  B   C"):
            name = keys.normalise_project(raw)
            if name:
                self.assertTrue(keys.validate_project(name),
                                "%r -> %r" % (raw, name))


class TestSimilarProjects(unittest.TestCase):
    def test_substring_both_directions(self):
        # The spec's own example: csac vs csac-data should warn.
        self.assertEqual(keys.similar_projects("csac-data", ["csac"]), ["csac"])
        self.assertEqual(keys.similar_projects("csac", ["csac-data"]),
                         ["csac-data"])

    def test_trailing_s(self):
        self.assertEqual(keys.similar_projects("csacs", ["csac"]), ["csac"])

    def test_hyphen_only_difference(self):
        self.assertEqual(keys.similar_projects("csac-data", ["csacdata"]),
                         ["csacdata"])

    def test_case_folded_existing(self):
        self.assertEqual(keys.similar_projects("csac", ["CSAC"]), ["CSAC"])

    def test_exact_match_excluded(self):
        self.assertEqual(keys.similar_projects("csac", ["csac"]), [])

    def test_unrelated_names_empty(self):
        self.assertEqual(
            keys.similar_projects("peacekeeping", ["csac", "aid-flows"]), [])


class TestFilenameChecks(unittest.TestCase):
    def test_clean_filename_has_no_problems(self):
        self.assertEqual(keys.filename_problems("csac-clean-2025.csv"), [])

    def test_spaces_flagged(self):
        problems = keys.filename_problems("my data.csv")
        self.assertTrue(any("space" in p for p in problems))

    def test_problem_chars_flagged(self):
        problems = keys.filename_problems('bad:file?.csv')
        self.assertTrue(any(":" in p for p in problems))
        self.assertTrue(any("?" in p for p in problems))

    def test_ampersand_not_flagged(self):
        # Spec §4 lists the exact set; & is not in it. Preservation wins.
        self.assertEqual(keys.filename_problems("a&b.csv"), [])

    def test_suggestion_hyphenates_spaces_and_strips_problem_chars(self):
        self.assertEqual(keys.suggest_filename('my data: v2?.csv'), "my-data-v2.csv")

    def test_suggestion_preserves_ampersand(self):
        self.assertEqual(keys.suggest_filename("a&b.csv"), "a&b.csv")


if __name__ == "__main__":
    unittest.main()
