import unittest

import keys


class TestBuildKey(unittest.TestCase):
    def test_happy_path(self):
        self.assertEqual(
            keys.build_key("rs2", "csac", "2_final", "green", "csac-clean-2025.csv"),
            "rs2/csac/2_final/green/csac-clean-2025.csv",
        )

    def test_key_uses_forward_slashes_only(self):
        key = keys.build_key("rs1", "treaty-texts", "0_raw", "amber", "a.txt")
        self.assertNotIn("\\", key)
        self.assertEqual(key.count("/"), 4)

    def test_invalid_strand_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs9", "csac", "2_final", "green", "a.csv")

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "csac", "final", "green", "a.csv")

    def test_red_raises_red_data_error(self):
        with self.assertRaises(keys.RedDataError):
            keys.build_key("rs2", "csac", "2_final", "red", "a.csv")

    def test_uppercase_project_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "CSAC", "2_final", "green", "a.csv")

    def test_empty_filename_rejected(self):
        with self.assertRaises(ValueError):
            keys.build_key("rs2", "csac", "2_final", "green", "")

    def test_sidecar_key(self):
        self.assertEqual(
            keys.sidecar_key("rs2/csac/2_final/green/csac-clean-2025.csv"),
            "rs2/csac/2_final/green/csac-clean-2025.csv.meta.json",
        )


class TestProjectValidation(unittest.TestCase):
    def test_valid_projects(self):
        for name in ("csac", "csac-data", "a1", "x-y-z2"):
            self.assertTrue(keys.validate_project(name), name)

    def test_invalid_projects(self):
        for name in ("CSAC", "csac data", "csac_", "-csac", "csac-", "", "és"):
            self.assertFalse(keys.validate_project(name), repr(name))


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
