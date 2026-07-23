import tempfile
import unittest
from pathlib import Path

import deposit


class TestResolveFiles(unittest.TestCase):
    def test_expands_glob_and_reports_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a.csv").write_text("x")
            (base / "b.csv").write_text("y")
            (base / "sub").mkdir()
            files, problems = deposit.resolve_files([str(base / "*.csv"),
                                                     str(base / "sub"),
                                                     str(base / "missing.txt")])
            self.assertEqual(sorted(f.name for f in files), ["a.csv", "b.csv"])
            self.assertEqual(len(problems), 2)  # dir rejected + miss reported


class TestHumanSize(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(deposit.human_size(500), "500 B")
        self.assertEqual(deposit.human_size(2048), "2.0 KB")
        self.assertEqual(deposit.human_size(5 * 1024 ** 3), "5.0 GB")


class TestPlanDeposits(unittest.TestCase):
    def test_builds_keys_and_sidecar_fields(self):
        meta = dict(strand="rs2", project="csac", state="2_final",
                    sensitivity="green", domain="quant", version="v1-0",
                    subjects=["armed-conflict"], abstract="a " * 120,
                    coverage_start="1989", coverage_end="2025",
                    vocabulary_version="2026-07-23")
        plans = deposit.plan_deposits([Path("data/x.csv")], meta, {})
        self.assertEqual(plans[0]["key"], "rs2/csac/2_final/green/x.csv")
        self.assertEqual(plans[0]["sidecar_key"],
                         "rs2/csac/2_final/green/x.csv.meta.json")
        self.assertEqual(plans[0]["fields"]["object_key"], plans[0]["key"])

    def test_per_file_overrides_take_precedence(self):
        meta = dict(strand="rs2", project="csac", state="2_final",
                    sensitivity="green", domain="quant", version="v1-0",
                    subjects=["armed-conflict"], abstract="a " * 120,
                    coverage_start="1989", coverage_end="2025",
                    vocabulary_version="2026-07-23")
        per_file = {"x.csv": {"coverage_start": "2001"}}
        plans = deposit.plan_deposits([Path("x.csv")], meta, per_file)
        self.assertEqual(plans[0]["fields"]["coverage_start"], "2001")


class TestPreview(unittest.TestCase):
    def test_truncates_after_three(self):
        plans = [{"path": Path("f%d.csv" % i),
                  "key": "rs2/p/0_raw/green/f%d.csv" % i,
                  "sidecar_key": "rs2/p/0_raw/green/f%d.csv.meta.json" % i,
                  "fields": {}} for i in range(5)]
        lines = deposit.preview_lines(plans)
        text = "\n".join(lines)
        self.assertIn("f0.csv", text)
        self.assertIn("2 more", text)
        self.assertNotIn("f4.csv", text)


class TestParser(unittest.TestCase):
    def test_defaults(self):
        args = deposit.build_parser().parse_args(["a.csv"])
        self.assertEqual(args.remote, "ceph")
        self.assertEqual(args.bucket, "crsw")
        self.assertFalse(args.dry_run)

    def test_all_flags(self):
        args = deposit.build_parser().parse_args(
            ["a.csv", "--strand", "rs2", "--project", "csac",
             "--state", "2_final", "--sensitivity", "green", "--dry-run"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.strand, "rs2")


if __name__ == "__main__":
    unittest.main()
