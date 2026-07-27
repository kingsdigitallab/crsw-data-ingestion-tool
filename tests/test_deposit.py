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
        self.assertEqual(plans[0]["key"], "rs2/csac/green/2_final/x.csv")
        self.assertEqual(plans[0]["sidecar_key"],
                         "rs2/csac/green/2_final/x.csv.meta.json")
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
                  "key": "rs2/p/green/0_raw/f%d.csv" % i,
                  "sidecar_key": "rs2/p/green/0_raw/f%d.csv.meta.json" % i,
                  "fields": {}} for i in range(5)]
        lines = deposit.preview_lines(plans)
        text = "\n".join(lines)
        self.assertIn("f0.csv", text)
        self.assertIn("2 more", text)
        self.assertNotIn("f4.csv", text)

    def test_preview_shows_sensitivity_before_state(self):
        # r3: the preview must show the reordered key the upload will use.
        meta = dict(strand="rs2", project="csac", state="2_final",
                    sensitivity="green", domain="quant", version="v1-0",
                    subjects=["armed-conflict"], abstract="a " * 120,
                    coverage_start="1989", coverage_end="2025",
                    vocabulary_version="2026-07-23")
        plans = deposit.plan_deposits([Path("x.csv")], meta, {})
        text = "\n".join(deposit.preview_lines(plans))
        self.assertIn("rs2/csac/green/2_final/x.csv", text)


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
    key = "rs2/csac/green/2_final/" + path.name
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


def _recording_copyto(uploaded, fail_on=None, exc=None):
    """copyto mock that records the true size of each 'uploaded' file."""
    def copyto(rclone, lp, rem, b, key, **kw):
        if fail_on and fail_on in key:
            raise exc
        uploaded[key] = Path(lp).stat().st_size
    return copyto


def _stat_matching(uploaded):
    """stat_key mock returning the true size of whatever copyto uploaded."""
    def stat(rclone, remote, bucket, key):
        if key in uploaded:
            return {"Name": key.rsplit("/", 1)[-1], "Size": uploaded[key]}
        return None
    return stat


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
        uploaded = {}
        order = []

        def copyto(rclone, lp, rem, b, key, **kw):
            order.append(key)
            uploaded[key] = Path(lp).stat().st_size

        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.stat_key",
                        side_effect=_stat_matching(uploaded)), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits("rclone", _Args(),
                                            [_plan_for(self.f1)], "tester")
        self.assertEqual(code, 0)
        self.assertEqual(len(order), 2)
        self.assertTrue(order[0].endswith("one.csv"))
        self.assertTrue(order[1].endswith("one.csv.meta.json"))

    def test_failure_stops_batch_and_reports(self):
        uploaded = {}
        copyto = _recording_copyto(
            uploaded, fail_on="two.csv",
            exc=deposit.transfer.TransferError("unreachable", "boom"))
        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.stat_key",
                        side_effect=_stat_matching(uploaded)), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), [_plan_for(self.f1), _plan_for(self.f2)], "t")
        self.assertNotEqual(code, 0)

    def test_verification_missing_object_is_error(self):
        with mock.patch("deposit.transfer.copyto"), \
             mock.patch("deposit.transfer.stat_key", return_value=None), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits("rclone", _Args(),
                                            [_plan_for(self.f1)], "t")
        self.assertNotEqual(code, 0)

    def test_size_mismatch_is_error_not_success(self):
        with mock.patch("deposit.transfer.copyto"), \
             mock.patch("deposit.transfer.stat_key",
                        return_value={"Name": "one.csv", "Size": 999999}), \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits("rclone", _Args(),
                                            [_plan_for(self.f1)], "t")
        self.assertNotEqual(code, 0)

    def test_sidecar_written_with_checksum_and_depositor(self):
        uploaded = {}
        captured = {}

        def copyto(rclone, lp, rem, b, key, **kw):
            uploaded[key] = Path(lp).stat().st_size
            if key.endswith(".meta.json"):
                import json
                captured.update(json.loads(Path(lp).read_text(encoding="utf-8")))

        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.stat_key",
                        side_effect=_stat_matching(uploaded)), \
             mock.patch("deposit.append_log"):
            deposit.perform_deposits("rclone", _Args(), [_plan_for(self.f1)],
                                     "njakeman")
        self.assertEqual(captured["depositor"], "njakeman")
        self.assertEqual(len(captured["checksum_sha256"]), 64)
        self.assertRegex(captured["deposited"], r"Z$")
        self.assertEqual(captured["schema_version"], "0.3")
        self.assertEqual(captured["source_type"], "archive")


    def test_interrupt_reports_completed_and_exits_130(self):
        uploaded = {}
        copyto = _recording_copyto(uploaded, fail_on="two.csv",
                                   exc=KeyboardInterrupt())
        with mock.patch("deposit.transfer.copyto", side_effect=copyto), \
             mock.patch("deposit.transfer.stat_key",
                        side_effect=_stat_matching(uploaded)), \
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

    def test_not_found_names_default_bucket(self):
        self.assertIn("crsw", deposit.MESSAGES["not_found"])

    def test_write_denied_formats_target(self):
        msg = deposit.MESSAGES["write_denied"].format(
            target="k1078591:crsw/rs2/x/green/0_raw/")
        self.assertIn("k1078591:crsw/rs2/x/green/0_raw/", msg)

    def test_permission_names_no_specific_strand(self):
        # The old canned "an RS2 credential cannot write to rs3/" example
        # sent a real user chasing the wrong strand. Never again.
        for key in ("permission", "write_denied"):
            lowered = deposit.MESSAGES[key].lower()
            for strand in ("rs1", "rs2", "rs3", "rs4"):
                self.assertNotIn(strand, lowered, key)


class TestResolveProjectChoice(unittest.TestCase):
    ENTRIES = [("1", "csac", ""), ("2", "aid-flows", "")]

    def test_n_and_new_return_sentinel(self):
        self.assertEqual(deposit.resolve_project_choice("n", self.ENTRIES), "new")
        self.assertEqual(deposit.resolve_project_choice(" NEW ", self.ENTRIES),
                         "new")

    def test_number_selects(self):
        self.assertEqual(deposit.resolve_project_choice("2", self.ENTRIES),
                         "aid-flows")

    def test_name_selects_case_insensitive(self):
        self.assertEqual(deposit.resolve_project_choice("CSAC", self.ENTRIES),
                         "csac")

    def test_unknown_is_none(self):
        self.assertIsNone(deposit.resolve_project_choice("nope", self.ENTRIES))


class TestPromptProject(unittest.TestCase):
    """Drives the picker via mocked input(); say/warn are captured so the
    honest-fallback wording is asserted, not just the return value."""

    def _run(self, listing, inputs):
        said, warned = [], []
        with mock.patch("builtins.input", side_effect=inputs) as inp, \
             mock.patch("deposit.say", side_effect=said.append), \
             mock.patch("deposit.warn", side_effect=warned.append):
            result = deposit.prompt_project("rs2", listing)
        return result, said, warned, inp

    def test_select_existing_by_number(self):
        result, _, _, _ = self._run(["csac", "aid-flows"], ["1"])
        self.assertEqual(result, "csac")

    def test_new_path_normalises_and_confirms(self):
        result, said, _, _ = self._run(["csac", "x"], ["n", "CSAC Data", "y"])
        self.assertEqual(result, "csac-data")
        self.assertIn("  -> normalised to: csac-data", said)

    def test_empty_listing_notes_and_creates_first(self):
        result, said, _, _ = self._run([], ["first-project", "y"])
        self.assertEqual(result, "first-project")
        self.assertTrue(any("No projects in rs2 yet" in s for s in said))

    def test_none_listing_notes_honestly(self):
        result, said, _, inp = self._run(None, ["solo-project", "y"])
        self.assertEqual(result, "solo-project")
        self.assertTrue(any("Couldn't list existing projects" in s
                            for s in said))
        prompts = [str(c.args[0]) for c in inp.call_args_list]
        self.assertTrue(any("Use project" in p for p in prompts))
        self.assertFalse(any("Create new" in p for p in prompts))

    def test_near_match_warns_and_decline_reprompts(self):
        result, _, warned, _ = self._run(
            ["csac"], ["n", "csacs", "n", "peacekeeping", "y"])
        self.assertEqual(result, "peacekeeping")
        self.assertTrue(any("csac" in w for w in warned))

    def test_legacy_invalid_name_reprompts(self):
        result, said, _, _ = self._run(["CSAC", "csac-data"], ["1", "2"])
        self.assertEqual(result, "csac-data")
        self.assertTrue(any("predates the naming rule" in s for s in said))


def _flagged_args():
    return deposit.build_parser().parse_args(
        ["a.csv", "--strand", "rs2", "--project", "csac", "--state", "2_final",
         "--sensitivity", "green", "--domain", "quant"])


class TestEarlyWriteProbe(unittest.TestCase):
    VOCAB = {"vocabulary_version": "x", "facets": {"contexts": ["a"]}}

    def test_denied_probe_fails_before_any_prompt(self):
        probed = []

        def prober(prefix):
            probed.append(prefix)
            return "permission"

        with mock.patch("builtins.input", side_effect=AssertionError), \
             mock.patch("deposit.say"):
            with self.assertRaises(deposit.transfer.TransferError) as ctx:
                deposit.prompt_metadata(_flagged_args(), self.VOCAB,
                                        None, prober)
        self.assertEqual(probed, ["rs2/csac/green/2_final"])
        self.assertEqual(ctx.exception.kind, "permission")
        self.assertEqual(ctx.exception.detail, "rs2/csac/green/2_final")

    def test_passing_probe_continues_interview(self):
        # Sentinel raised by the NEXT prompt (version) proves the probe
        # passed and the interview moved on.
        class PastProbe(Exception):
            pass

        with mock.patch("builtins.input", side_effect=PastProbe), \
             mock.patch("deposit.say"):
            with self.assertRaises(PastProbe):
                deposit.prompt_metadata(_flagged_args(), self.VOCAB,
                                        None, lambda prefix: None)


class TestSettingSources(unittest.TestCase):
    def test_flags_win(self):
        args = deposit.build_parser().parse_args(
            ["a.csv", "--remote", "k1", "--bucket", "b1"])
        self.assertEqual(deposit.setting_sources(args, {"remote": "ceph"}),
                         ("--remote flag", "--bucket flag"))

    def test_config_next(self):
        args = deposit.build_parser().parse_args(["a.csv"])
        self.assertEqual(
            deposit.setting_sources(args, {"remote": "ceph", "bucket": "crsw"}),
            ("saved config", "saved config"))

    def test_defaults_last_and_mixed(self):
        args = deposit.build_parser().parse_args(["a.csv", "--remote", "k1"])
        self.assertEqual(deposit.setting_sources(args, {}),
                         ("--remote flag", "default"))


class TestCheckProjectFlag(unittest.TestCase):
    def test_existing_project_silent(self):
        with mock.patch("builtins.input", side_effect=AssertionError):
            deposit.check_project_flag("csac", "rs2", ["csac"], False)

    def test_no_listing_silent(self):
        with mock.patch("builtins.input", side_effect=AssertionError):
            deposit.check_project_flag("newproj", "rs2", None, False)

    def test_new_project_confirms_exactly_once(self):
        with mock.patch("builtins.input", side_effect=["y"]) as inp:
            deposit.check_project_flag("newproj", "rs2", ["csac"], False)
        self.assertEqual(inp.call_count, 1)

    def test_decline_raises_valueerror(self):
        with mock.patch("builtins.input", side_effect=["n"]):
            with self.assertRaises(ValueError):
                deposit.check_project_flag("newproj", "rs2", ["csac"], False)

    def test_dry_run_never_prompts_prints_note(self):
        said = []
        with mock.patch("builtins.input", side_effect=AssertionError), \
             mock.patch("deposit.say", side_effect=said.append):
            deposit.check_project_flag("csac-data", "rs2", ["csac"], True)
        self.assertTrue(any("does not exist in rs2" in s for s in said))
        self.assertTrue(any("similar to existing 'csac'" in s for s in said))


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
