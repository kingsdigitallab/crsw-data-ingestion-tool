import unittest

from crsw_deposit import deposit_logic, record

META = {
    "strand": "rs2", "project": "csac", "sensitivity": "green",
    "state": "2_final", "dataset": "poc", "domain": "quant",
    "version": "1-0", "abstract": "words " * 60,
    "subject": ["forced-labour"],
    "coverage_start": "2020", "coverage_end": "2021",
    "license": "CC-BY-4.0",
}
NOW = "2026-09-18T10:00:00Z"
LATER = "2026-09-19T10:00:00Z"
UUID = "12345678-1234-4123-8123-123456789abc"


def entry(path, checksum="a" * 64, size=1, temporal=None):
    return record.manifest_entry(path, checksum, size, temporal=temporal)


class TestPlanKeys(unittest.TestCase):
    def test_keys_in_input_order(self):
        out = deposit_logic.plan_keys(META, ["b.csv", "sub/a.csv"])
        self.assertEqual(out, [
            ("b.csv", "rs2/csac/green/2_final/poc/b.csv"),
            ("sub/a.csv", "rs2/csac/green/2_final/poc/sub/a.csv"),
        ])

    def test_collision_names_display_labels(self):
        with self.assertRaises(ValueError) as cm:
            deposit_logic.plan_keys(META, ["x.csv", "x.csv"],
                                    display=["a/x.csv", "b/x.csv"])
        self.assertIn("a/x.csv and b/x.csv", str(cm.exception))

    def test_collision_defaults_to_member_names(self):
        with self.assertRaises(ValueError) as cm:
            deposit_logic.plan_keys(META, ["x.csv", "x.csv"])
        self.assertIn("x.csv and x.csv", str(cm.exception))

    def test_red_refused(self):
        from crsw_deposit import keys
        with self.assertRaises(keys.RedDataError):
            deposit_logic.plan_keys(dict(META, sensitivity="red"), ["a"])


class TestDatasetUuid(unittest.TestCase):
    def test_first_deposit_mints(self):
        self.assertEqual(
            deposit_logic.dataset_uuid_for(None, mint=lambda: "new"), "new")

    def test_existing_record_keeps_uuid(self):
        self.assertEqual(
            deposit_logic.dataset_uuid_for({"dataset_uuid": "old"},
                                           mint=lambda: "new"), "old")


class TestAssembleRecord(unittest.TestCase):
    def test_first_deposit(self):
        rec, union, added, updated = deposit_logic.assemble_record(
            META, None, [entry("a.csv")], "k1078591", NOW, UUID)
        self.assertEqual(rec["dataset_uuid"], UUID)
        self.assertEqual(rec["identifier"], "rs2/csac/green/2_final/poc")
        self.assertEqual(rec["created"], NOW)
        self.assertEqual(rec["modified"], NOW)
        self.assertEqual(rec["depositors"], ["k1078591"])
        self.assertEqual(rec["temporal"], {"start": "2020", "end": "2021"})
        self.assertEqual([e["path"] for e in union], ["a.csv"])
        self.assertEqual(added, ["a.csv"])
        self.assertEqual(updated, [])
        errors, _ = record.validate_record(rec, {"forced-labour"})
        self.assertEqual(errors, [])

    def test_repeat_deposit_preserves_created_and_accumulates(self):
        existing = {
            "dataset_uuid": UUID, "created": NOW,
            "depositors": ["alice"],
            "temporal": {"start": "2019", "end": "2021"},
            "files": [entry("a.csv", checksum="b" * 64)],
        }
        rec, union, added, updated = deposit_logic.assemble_record(
            META, existing, [entry("a.csv"), entry("c.csv")],
            "k1078591", LATER, UUID)
        self.assertEqual(rec["created"], NOW)
        self.assertEqual(rec["modified"], LATER)
        self.assertEqual(rec["depositors"], ["alice", "k1078591"])
        # Old envelope kept containment: 2019 < meta's 2020.
        self.assertEqual(rec["temporal"], {"start": "2019", "end": "2021"})
        self.assertEqual([e["path"] for e in union], ["a.csv", "c.csv"])
        self.assertEqual(added, ["c.csv"])
        self.assertEqual(updated, ["a.csv"])

    def test_created_hint_used_only_for_first_deposit(self):
        rec, _, _, _ = deposit_logic.assemble_record(
            META, None, [entry("a.csv")], "d", LATER, UUID, created=NOW)
        self.assertEqual((rec["created"], rec["modified"]), (NOW, LATER))
        existing = {"dataset_uuid": UUID, "created": "2020-01-01T00:00:00Z",
                    "temporal": {"start": "2020", "end": "2021"},
                    "files": [entry("old.csv")]}
        rec, _, _, _ = deposit_logic.assemble_record(
            META, existing, [entry("a.csv")], "d", LATER, UUID, created=NOW)
        self.assertEqual(rec["created"], "2020-01-01T00:00:00Z")

    def test_envelope_widens_over_member_coverage(self):
        rec, _, _, _ = deposit_logic.assemble_record(
            META, None,
            [entry("a.csv", temporal={"start": "1990", "end": "2025-06-30"})],
            "d", NOW, UUID)
        self.assertEqual(rec["temporal"],
                         {"start": "1990", "end": "2025-06-30"})

    def test_never_removes_existing_members(self):
        existing = {"dataset_uuid": UUID, "created": NOW,
                    "temporal": {"start": "2020", "end": "2021"},
                    "files": [entry("old.csv")]}
        rec, _, _, _ = deposit_logic.assemble_record(
            META, existing, [entry("new.csv")], "d", LATER, UUID)
        self.assertEqual([e["path"] for e in rec["files"]],
                         ["new.csv", "old.csv"])


class TestRecordBytes(unittest.TestCase):
    def test_lf_only_utf8_trailing_newline(self):
        rec, _, _, _ = deposit_logic.assemble_record(
            dict(META, abstract="café " * 60), None, [entry("a.csv")],
            "d", NOW, UUID)
        data = deposit_logic.record_bytes(rec)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        self.assertIn("café".encode("utf-8"), data)  # ensure_ascii off
        self.assertEqual(data.decode("utf-8"), record.record_json(rec))


class TestCompletionProblems(unittest.TestCase):
    def test_complete(self):
        sizes = {"p/a.csv": 3, "p/b.csv": 5}
        problems = deposit_logic.completion_problems(
            "p", [entry("a.csv", size=3), entry("b.csv", size=5)], sizes.get)
        self.assertEqual(problems, [])

    def test_missing_and_mismatch_reported_per_key(self):
        sizes = {"p/a.csv": 4}
        problems = deposit_logic.completion_problems(
            "p", [entry("a.csv", size=3), entry("b.csv", size=5)], sizes.get)
        self.assertEqual(problems, [
            "size mismatch at p/a.csv: expected 3, stored 4",
            "no object at p/b.csv",
        ])


if __name__ == "__main__":
    unittest.main()
