import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def _plan_for(path: Path):
    key = "rs2/csac/2_final/green/" + path.name
    return {"path": path, "key": key, "sidecar_key": key + ".meta.json",
            "fields": dict(object_key=key, strand="rs2", domain="quant",
                           project="csac", state="2_final", sensitivity="green",
                           coverage_start="1989", coverage_end="2025",
                           version="v1-0", abstract="a " * 120,
                           subjects=["armed-conflict"],
                           vocabulary_version="2026-07-23")}


class _Args:
    remote = "ceph"
    bucket = "crsw"
    verbose = False
    dry_run = False


class TestPerformDeposits(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.f1 = Path(self._tmp.name) / "one.csv"
        self.f1.write_text("1")
        self.f2 = Path(self._tmp.name) / "two.csv"
        self.f2.write_text("2")

    def tearDown(self):
        self._tmp.cleanup()

    def test_data_uploaded_before_sidecar(self):
        order = []
        with mock.patch("deposit.transfer.copyto",
                        side_effect=lambda r, lp, rem, b, key, **kw: order.append(key)), \
             mock.patch("deposit.transfer.key_exists", return_value=True), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits("rclone", _Args(),
                                            [_plan_for(self.f1)], "tester")
        self.assertEqual(code, 0)
        self.assertEqual(len(order), 2)
        self.assertTrue(order[0].endswith("one.csv"))
        self.assertTrue(order[1].endswith("one.csv.meta.json"))

    def test_failure_stops_batch_and_reports(self):
        def copyto(rclone, lp, rem, b, key, **kw):
            if "two.csv" in key:
                raise deposit.transfer.TransferError("unreachable", "boom")
        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.key_exists", return_value=True), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), [_plan_for(self.f1), _plan_for(self.f2)], "t")
        self.assertNotEqual(code, 0)

    def test_verification_failure_is_an_error(self):
        with mock.patch("deposit.transfer.copyto"), \
             mock.patch("deposit.transfer.key_exists", return_value=False), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits("rclone", _Args(),
                                            [_plan_for(self.f1)], "t")
        self.assertNotEqual(code, 0)

    def test_sidecar_written_with_checksum_and_depositor(self):
        captured = {}

        def copyto(rclone, lp, rem, b, key, **kw):
            if key.endswith(".meta.json"):
                import json
                captured.update(json.loads(Path(lp).read_text(encoding="utf-8")))

        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.key_exists", return_value=True), \
             mock.patch("deposit.append_log"):
            deposit.perform_deposits("rclone", _Args(), [_plan_for(self.f1)],
                                     "njakeman")
        self.assertEqual(captured["depositor"], "njakeman")
        self.assertEqual(len(captured["checksum_sha256"]), 64)
        self.assertRegex(captured["deposited"], r"Z$")


    def test_interrupt_reports_completed_and_exits_130(self):
        def copyto(rclone, lp, rem, b, key, **kw):
            if "two.csv" in key:
                raise KeyboardInterrupt()
        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.key_exists", return_value=True), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), [_plan_for(self.f1), _plan_for(self.f2)], "t")
        self.assertEqual(code, 130)


class TestMessageQuality(unittest.TestCase):
    """Spec §8 finish test: if a message mentions sockets, TLS, or an S3
    error code, it isn't finished."""
    FORBIDDEN = ("errno", "traceback", "socket", "tls", "ssl",
                 "xml", "getaddrinfo", "boto", "http status")

    def test_no_jargon_in_user_messages(self):
        for kind, message in deposit.MESSAGES.items():
            lowered = message.lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, lowered, "%s mentions %r" % (kind, word))

    def test_messages_are_ascii(self):
        # Managed Windows consoles use cp1252; non-ASCII garbles to '?'.
        for kind, message in deposit.MESSAGES.items():
            message.encode("ascii")

    def test_unreachable_names_the_vpn(self):
        self.assertIn("VPN", deposit.MESSAGES["unreachable"])

    def test_no_rclone_has_download_url(self):
        self.assertIn("https://rclone.org", deposit.MESSAGES["no_rclone"])

    def test_no_remote_has_config_stanza(self):
        self.assertIn("type = s3", deposit.MESSAGES["no_remote"])

    def test_red_points_to_tre(self):
        self.assertIn("TRE", deposit.MESSAGES["red_refused"])


if __name__ == "__main__":
    unittest.main()
