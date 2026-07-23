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
        self.assertIsNone(args.remote)
        self.assertIsNone(args.bucket)
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
                           version="1-0", abstract="a " * 120,
                           subjects=["armed-conflict"],
                           source_type="archive", source_detail="test fixture",
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
        self.assertEqual(captured["schema_version"], "0.3")
        self.assertEqual(captured["source_type"], "archive")


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


class TestSubjectSelection(unittest.TestCase):
    VOCAB = {"vocabulary_version": "x",
             "facets": {"practices": ["forced-labour", "sexual-slavery"],
                        "contexts": ["armed-conflict"]}}

    def test_entries_numbered_continuously_across_facets(self):
        entries = deposit.subject_entries(self.VOCAB)
        self.assertEqual(entries[0], ("1", "forced-labour", "practices"))
        self.assertEqual(entries[2], ("3", "armed-conflict", "contexts"))

    def test_resolve_numbers_terms_and_mix(self):
        entries = deposit.subject_entries(self.VOCAB)
        resolved, unknown = deposit.resolve_subjects("1, 3, sexual-slavery", entries)
        self.assertEqual(resolved,
                         ["forced-labour", "armed-conflict", "sexual-slavery"])
        self.assertEqual(unknown, [])

    def test_unknown_tokens_named_not_dropped(self):
        entries = deposit.subject_entries(self.VOCAB)
        resolved, unknown = deposit.resolve_subjects("1, dragons, 99", entries)
        self.assertEqual(resolved, ["forced-labour"])
        self.assertEqual(unknown, ["dragons", "99"])

    def test_duplicates_collapse(self):
        entries = deposit.subject_entries(self.VOCAB)
        resolved, _ = deposit.resolve_subjects("1, forced-labour, 1", entries)
        self.assertEqual(resolved, ["forced-labour"])

    def test_listing_single_column_when_narrow(self):
        entries = deposit.subject_entries(self.VOCAB)
        lines = deposit.subject_listing_lines(entries, width=80)
        body = [l for l in lines if "[" in l]
        self.assertEqual(len(body), 3)  # one term per line

    def test_listing_two_columns_when_wide(self):
        entries = deposit.subject_entries(self.VOCAB)
        lines = deposit.subject_listing_lines(entries, width=120)
        body = [l for l in lines if "[" in l]
        self.assertLess(len(body), 3)  # terms share lines

    def test_facet_headings_present(self):
        lines = deposit.subject_listing_lines(
            deposit.subject_entries(self.VOCAB), width=80)
        text = "\n".join(lines)
        self.assertIn("practices", text)
        self.assertIn("contexts", text)


class TestDomainFlag(unittest.TestCase):
    def test_parser_accepts_domain(self):
        args = deposit.build_parser().parse_args(["a.csv", "--domain", "quant"])
        self.assertEqual(args.domain, "quant")


class TestConfig(unittest.TestCase):
    def test_load_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(deposit.load_config(Path(d) / "nope.json"), {})

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "config.json"
            deposit.save_config({"remote": "s3_kcl_neil", "bucket": "neil-test-01"}, p)
            self.assertEqual(deposit.load_config(p)["remote"], "s3_kcl_neil")

    def test_malformed_config_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(deposit.load_config(p), {})

    def test_precedence_flag_config_default(self):
        args = deposit.build_parser().parse_args(["a.csv", "--remote", "flagged"])
        cfg = {"remote": "configured", "bucket": "cfg-bucket"}
        self.assertEqual(deposit.resolve_settings(args, cfg),
                         ("flagged", "cfg-bucket"))
        args2 = deposit.build_parser().parse_args(["a.csv"])
        self.assertEqual(deposit.resolve_settings(args2, {}), ("ceph", "crsw"))

    def test_config_path_dirname(self):
        self.assertEqual(deposit.config_path().name, "config.json")
        self.assertEqual(deposit.config_path().parent.name, "crsw-deposit")


class TestNoRemoteMessage(unittest.TestCase):
    def test_lists_existing_remotes(self):
        msg = deposit.MESSAGES["no_remote"].format(remote="ceph",
                                                   remotes="s3_kcl_neil, other")
        self.assertIn("s3_kcl_neil", msg)


class TestStyle(unittest.TestCase):
    def test_no_color_disables(self):
        with mock.patch.dict("deposit.os.environ",
                             {"NO_COLOR": "1", "FORCE_COLOR": "1"}, clear=False):
            self.assertFalse(deposit.colour_enabled())
            self.assertEqual(deposit.style("x", "red"), "x")

    def test_force_color_enables(self):
        with mock.patch.dict("deposit.os.environ", {"FORCE_COLOR": "1"},
                             clear=True):
            self.assertTrue(deposit.colour_enabled())
            self.assertEqual(deposit.style("x", "red"), "\x1b[31mx\x1b[0m")

    def test_multiple_styles_combined(self):
        with mock.patch.dict("deposit.os.environ", {"FORCE_COLOR": "1"},
                             clear=True):
            self.assertEqual(deposit.style("x", "bold", "cyan"),
                             "\x1b[1;36mx\x1b[0m")

    def test_not_a_tty_disables(self):
        with mock.patch.dict("deposit.os.environ", {}, clear=True), \
             mock.patch("deposit.sys.stdout") as out:
            out.isatty.return_value = False
            self.assertFalse(deposit.colour_enabled())

    def test_enable_vt_never_raises(self):
        deposit.enable_vt()  # must be safe on every platform


class TestResolveChoice(unittest.TestCase):
    STRAND = [("1", "rs1", ""), ("2", "rs2", ""), ("3", "rs3", ""), ("4", "rs4", "")]
    STATE = [("0", "0_raw", "raw, as received"),
             ("1", "1_interim", "in progress"),
             ("2", "2_final", "released or shared")]

    def test_number_resolves(self):
        self.assertEqual(deposit.resolve_choice("2", self.STRAND), "rs2")

    def test_literal_resolves(self):
        self.assertEqual(deposit.resolve_choice("rs2", self.STRAND), "rs2")

    def test_case_insensitive(self):
        self.assertEqual(deposit.resolve_choice("RS2", self.STRAND), "rs2")

    def test_state_numbers_are_ordinals(self):
        self.assertEqual(deposit.resolve_choice("0", self.STATE), "0_raw")
        self.assertEqual(deposit.resolve_choice("2", self.STATE), "2_final")

    def test_whitespace_stripped(self):
        self.assertEqual(deposit.resolve_choice("  2 ", self.STRAND), "rs2")

    def test_invalid_returns_none(self):
        self.assertIsNone(deposit.resolve_choice("5", self.STRAND))
        self.assertIsNone(deposit.resolve_choice("rs9", self.STRAND))
        self.assertIsNone(deposit.resolve_choice("", self.STRAND))


class TestChoiceLines(unittest.TestCase):
    def test_hintless_entries_share_a_line(self):
        lines = deposit.choice_lines("Strand", TestResolveChoice.STRAND)
        body = "\n".join(lines)
        self.assertIn("Strand", lines[0])
        self.assertIn("[1] rs1", body)
        self.assertIn("[4] rs4", body)
        self.assertEqual(len(lines), 2)  # title + one option line

    def test_hinted_entries_one_per_line(self):
        lines = deposit.choice_lines("State", TestResolveChoice.STATE)
        self.assertEqual(len(lines), 4)  # title + 3 options
        self.assertTrue(any("released or shared" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
