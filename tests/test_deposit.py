import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deposit


class TestResolveFiles(unittest.TestCase):
    def test_expands_glob_and_walks_dirs(self):
        # r7 §1: directories are no longer rejected - they are walked,
        # and a miss is the only remaining problem.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a.csv").write_text("x")
            (base / "b.csv").write_text("y")
            (base / "sub").mkdir()
            (base / "sub" / "nested.csv").write_text("z")
            sources, problems, notes = deposit.resolve_files(
                [str(base / "*.csv"), str(base / "sub"),
                 str(base / "missing.txt")])
            self.assertEqual(sorted(s.member for s in sources),
                             ["a.csv", "b.csv", "nested.csv"])
            self.assertEqual(len(problems), 1)
            self.assertIn("missing.txt", problems[0])
            self.assertTrue(any(str(base / "sub") in n for n in notes))

    def test_bare_folder_preserves_relative_structure(self):
        # r7 §1's worked example: survey-2024/2024/tiles/a.tif ->
        # member "2024/tiles/a.tif", the folder's own name dropped.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "survey-2024"
            (base / "2024" / "tiles").mkdir(parents=True)
            (base / "2024" / "tiles" / "a.tif").write_text("x")
            (base / "readme.md").write_text("y")
            sources, problems, notes = deposit.resolve_files([str(base)])
            self.assertEqual(problems, [])
            self.assertEqual(sorted(s.member for s in sources),
                             ["2024/tiles/a.tif", "readme.md"])

    def test_same_basename_different_subfolders_no_collision(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "coastal"
            (base / "2023").mkdir(parents=True)
            (base / "2024").mkdir(parents=True)
            (base / "2023" / "a.tif").write_text("x")
            (base / "2024" / "a.tif").write_text("y")
            sources, problems, notes = deposit.resolve_files([str(base)])
            self.assertEqual(sorted(s.member for s in sources),
                             ["2023/a.tif", "2024/a.tif"])

    def test_wildcard_across_subfolders_anchors_at_literal_prefix(self):
        # data/*/results.csv -> root "data" -> "site1/results.csv" etc.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "data"
            for site in ("site1", "site2"):
                (base / site).mkdir(parents=True)
                (base / site / "results.csv").write_text(site)
            pattern = str(base / "*" / "results.csv")
            sources, problems, notes = deposit.resolve_files([pattern])
            self.assertEqual(sorted(s.member for s in sources),
                             ["site1/results.csv", "site2/results.csv"])

    def test_explicit_file_argument_unchanged(self):
        # Backward compatibility: an explicit file's member is still just
        # its basename, exactly as before r7.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "a" / "b"
            base.mkdir(parents=True)
            (base / "c.txt").write_text("x")
            sources, problems, notes = deposit.resolve_files(
                [str(base / "c.txt")])
            self.assertEqual([s.member for s in sources], ["c.txt"])

    def test_double_star_recurses(self):
        # '**' without recursive=True used to collapse to exactly one
        # level (verified against Python's own glob module); the member
        # is relative to the literal prefix before the wildcard ("d"),
        # same anchoring rule as any other wildcard pattern.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a" / "b").mkdir(parents=True)
            (base / "a" / "b" / "deep.csv").write_text("x")
            sources, problems, notes = deposit.resolve_files(
                [str(base / "**" / "*.csv")])
            self.assertEqual([s.member for s in sources], ["a/b/deep.csv"])

    def test_literal_bracket_filename_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "data[1].csv").write_text("x")
            sources, problems, notes = deposit.resolve_files(
                [str(base / "data[1].csv")])
            self.assertEqual([s.member for s in sources], ["data[1].csv"])
            self.assertTrue(any("literally" in n for n in notes))

    def test_noise_files_excluded_by_default_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "tree"
            base.mkdir()
            (base / "a.csv").write_text("x")
            (base / ".DS_Store").write_text("junk")
            (base / "._a.csv").write_text("junk")
            sources, problems, notes = deposit.resolve_files([str(base)])
            self.assertEqual([s.member for s in sources], ["a.csv"])
            self.assertTrue(any("Excluded 2" in n for n in notes))

    def test_include_noise_keeps_them(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "tree"
            base.mkdir()
            (base / "a.csv").write_text("x")
            (base / ".DS_Store").write_text("junk")
            sources, problems, notes = deposit.resolve_files(
                [str(base)], include_noise=True)
            self.assertEqual(sorted(s.member for s in sources),
                             [".DS_Store", "a.csv"])

    def test_reserved_name_nested_in_folder_still_flagged(self):
        # r7 §3: reservation applies at every depth, not just the last
        # segment - confirmed here via the member path produced by a
        # folder walk (the caller in main() runs is_reserved_member on
        # this exact string).
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "tree"
            (base / "sub").mkdir(parents=True)
            (base / "sub" / "dataset.foo.json").write_text("{}")
            sources, problems, notes = deposit.resolve_files([str(base)])
            self.assertTrue(any(deposit.keys.is_reserved_member(s.member)
                                for s in sources))


class TestHumanSize(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(deposit.human_size(500), "500 B")
        self.assertEqual(deposit.human_size(2048), "2.0 KB")
        self.assertEqual(deposit.human_size(5 * 1024 ** 3), "5.0 GB")


_META = dict(strand="rs2", project="csac", dataset="sentinel2-imagery",
             state="2_final",
             sensitivity="green", domain="quant", version="1-0",
             subject=["armed-conflict"], abstract="a " * 120,
             coverage_start="1989", coverage_end="2025",
             source_type="archive", source_detail="test fixture",
             license="CC-BY-4.0", vocabulary_version="2026-07-23")

_VOCAB = {"vocabulary_version": "2026-07-23",
          "domains": [{"code": "quant", "label": "Quantitative",
                       "steward": "Tester"}],
          "facets": {"themes": ["armed-conflict"]}}


def _source(path_str, member=None):
    """A Source for a bare local path string, mirroring what
    resolve_files would produce for an explicit file argument."""
    p = Path(path_str)
    return deposit.Source(p, member if member is not None else p.name)


class TestPlanDeposits(unittest.TestCase):
    def test_builds_keys_and_overrides(self):
        per_file = {"x.csv": {"coverage_start": "2001"}}
        plans = deposit.plan_deposits([_source("data/x.csv")], dict(_META),
                                      per_file)
        self.assertEqual(plans[0]["key"],
                         "rs2/csac/green/2_final/sentinel2-imagery/x.csv")
        self.assertEqual(plans[0]["member"], "x.csv")
        self.assertEqual(plans[0]["entry_overrides"],
                         {"coverage_start": "2001"})

    def test_duplicate_keys_refused(self):
        # Same filename from two EXPLICIT file arguments would silently
        # overwrite - member is the basename for both.
        with self.assertRaises(ValueError):
            deposit.plan_deposits([_source("a/x.csv"), _source("b/x.csv")],
                                  dict(_META), {})

    def test_same_basename_different_subpaths_no_collision(self):
        # r7 §1: two files with the same basename under different
        # sub-folders of ONE folder argument keep their sub-path, so
        # they no longer collide the way two flat same-named files do.
        deposit.plan_deposits(
            [_source("2023/a.tif", "2023/a.tif"),
             _source("2024/a.tif", "2024/a.tif")], dict(_META), {})

    def test_same_filename_in_two_datasets_no_collision(self):
        # r6 §1: the dataset element removes the filename-collision
        # hazard entirely - two datasets can each have a readme.md.
        meta_a = dict(_META, dataset="sentinel2-imagery")
        meta_b = dict(_META, dataset="training-labels")
        plan_a = deposit.plan_deposits([_source("readme.md")], meta_a, {})
        plan_b = deposit.plan_deposits([_source("readme.md")], meta_b, {})
        self.assertNotEqual(plan_a[0]["key"], plan_b[0]["key"])
        self.assertEqual(plan_a[0]["key"],
                         "rs2/csac/green/2_final/sentinel2-imagery/readme.md")
        self.assertEqual(plan_b[0]["key"],
                         "rs2/csac/green/2_final/training-labels/readme.md")


class TestSetMember(unittest.TestCase):
    def test_rewrites_key_and_member_together(self):
        plan = {"path": Path("x.csv"), "member": "x.csv",
                "key": "rs2/csac/green/2_final/sentinel2-imagery/x.csv",
                "entry_overrides": {}}
        deposit.set_member(plan, dict(_META), "2024/x.csv")
        self.assertEqual(plan["member"], "2024/x.csv")
        self.assertEqual(
            plan["key"],
            "rs2/csac/green/2_final/sentinel2-imagery/2024/x.csv")

    def test_invariant_key_equals_prefix_plus_member(self):
        # The completion check reconstructs every key as
        # prefix + "/" + entry["path"] - this must hold for every plan.
        meta = dict(_META)
        prefix = deposit.keys.dataset_prefix(
            meta["strand"], meta["project"], meta["sensitivity"],
            meta["state"], meta["dataset"])
        plans = deposit.plan_deposits(
            [_source("a.csv"), _source("2024/x.csv", "2024/x.csv")],
            meta, {})
        for plan in plans:
            self.assertEqual(plan["key"], prefix + "/" + plan["member"])


class TestPrepareEntries(unittest.TestCase):
    def test_checksums_and_formats(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_text("1,2,3")
            plans = deposit.plan_deposits([_source(str(p), "x.csv")],
                                          dict(_META), {})
            entries = deposit.prepare_entries(plans)
        self.assertEqual(entries[0]["path"], "x.csv")
        self.assertEqual(entries[0]["bytes"], 5)
        self.assertEqual(len(entries[0]["checksum_sha256"]), 64)
        self.assertEqual(entries[0]["format"], "text/csv")

    def test_nested_member_becomes_manifest_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.tif"
            p.write_text("x")
            plans = deposit.plan_deposits(
                [_source(str(p), "2024/tiles/a.tif")], dict(_META), {})
            entries = deposit.prepare_entries(plans)
        self.assertEqual(entries[0]["path"], "2024/tiles/a.tif")

    def test_entry_uses_renamed_object_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad name.csv"
            p.write_text("x")
            plan = {"path": p, "member": "bad-name.csv",
                    "key": "rs2/csac/green/2_final/sentinel2-imagery/bad-name.csv",
                    "entry_overrides": {"coverage_start": "1990"}}
            entries = deposit.prepare_entries([plan])
        self.assertEqual(entries[0]["path"], "bad-name.csv")
        self.assertEqual(entries[0]["temporal"], {"start": "1990", "end": None})


class TestClassifyMembers(unittest.TestCase):
    ENTRY = {"path": "a.csv", "checksum_sha256": "ab" * 32, "bytes": 1}

    def test_first_deposit_all_added(self):
        added, updated, unchanged = deposit.classify_members([self.ENTRY], None)
        self.assertEqual((added, updated, unchanged), (["a.csv"], [], []))

    def test_against_existing_record(self):
        existing = {"files": [dict(self.ENTRY),
                              {"path": "b.csv", "checksum_sha256": "cd" * 32,
                               "bytes": 2}]}
        batch = [dict(self.ENTRY),
                 {"path": "b.csv", "checksum_sha256": "ee" * 32, "bytes": 2},
                 {"path": "c.csv", "checksum_sha256": "ab" * 32, "bytes": 3}]
        added, updated, unchanged = deposit.classify_members(batch, existing)
        self.assertEqual(added, ["c.csv"])
        self.assertEqual(updated, ["b.csv"])
        self.assertEqual(unchanged, ["a.csv"])


class TestDuplicateContentWarnings(unittest.TestCase):
    def test_warns_when_checksum_matches_a_different_existing_path(self):
        # r7 §1: a file re-deposited from inside a folder gets a NEW
        # manifest path, so it is added rather than updated - warn
        # before the point of no return rather than silently doubling.
        existing = {"files": [{"path": "readme.md",
                               "checksum_sha256": "ab" * 32, "bytes": 1}]}
        entries = [{"path": "2024/readme.md",
                   "checksum_sha256": "ab" * 32, "bytes": 1}]
        warnings = deposit.duplicate_content_warnings(entries, existing)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2024/readme.md", warnings[0])
        self.assertIn("readme.md", warnings[0])

    def test_no_warning_for_the_same_path(self):
        existing = {"files": [{"path": "a.csv",
                               "checksum_sha256": "ab" * 32, "bytes": 1}]}
        entries = [{"path": "a.csv", "checksum_sha256": "ab" * 32, "bytes": 1}]
        self.assertEqual(deposit.duplicate_content_warnings(entries, existing), [])

    def test_no_existing_record_no_warnings(self):
        entries = [{"path": "a.csv", "checksum_sha256": "ab" * 32, "bytes": 1}]
        self.assertEqual(deposit.duplicate_content_warnings(entries, None), [])


class TestPreview(unittest.TestCase):
    def _plans(self, n):
        return [{"path": Path("f%d.csv" % i), "member": "f%d.csv" % i,
                 "key": "rs2/csac/green/2_final/sentinel2-imagery/f%d.csv" % i,
                 "entry_overrides": {}} for i in range(n)]

    def test_first_deposit_shape_and_truncation(self):
        plans = self._plans(5)
        classification = (["f%d.csv" % i for i in range(5)], [], [])
        text = "\n".join(deposit.preview_lines(plans, dict(_META), None,
                                               classification))
        self.assertIn(
            "rs2/csac/green/2_final/sentinel2-imagery/"
            "dataset.sentinel2-imagery.json", text)
        self.assertIn("first deposit", text)
        self.assertIn("5 added, 0 updated, 0 unchanged", text)
        self.assertIn("f0.csv", text)
        self.assertIn("2 more", text)
        self.assertNotIn("f4.csv", text)

    def test_preview_shows_sensitivity_before_state(self):
        # r3: the preview must show the reordered key the upload will use.
        plans = self._plans(1)
        text = "\n".join(deposit.preview_lines(plans, dict(_META), None,
                                               (["f0.csv"], [], [])))
        self.assertIn("rs2/csac/green/2_final/sentinel2-imagery/f0.csv", text)

    def test_existing_record_counts_and_overwrite_note(self):
        plans = self._plans(2)
        existing = {"files": [{"path": "old.csv",
                               "checksum_sha256": "ab" * 32, "bytes": 1}]}
        classification = (["f1.csv"], ["f0.csv"], [])
        text = "\n".join(deposit.preview_lines(plans, dict(_META), existing,
                                               classification))
        self.assertIn("updates existing, 1 -> 2 files", text)
        self.assertIn("1 added, 1 updated, 0 unchanged", text)
        self.assertIn("new version; previous kept by bucket versioning", text)

    def test_unchanged_members_labelled_skipped(self):
        plans = self._plans(1)
        existing = {"files": [{"path": "f0.csv",
                               "checksum_sha256": "ab" * 32, "bytes": 1}]}
        text = "\n".join(deposit.preview_lines(plans, dict(_META), existing,
                                               ([], [], ["f0.csv"])))
        self.assertIn("unchanged - upload will be skipped", text)


    def test_origin_lines_only_when_present(self):
        plans = self._plans(1)
        meta = dict(_META)
        text = "\n".join(deposit.preview_lines(plans, meta, None,
                                               (["f0.csv"], [], [])))
        self.assertNotIn("derived from", text)
        self.assertNotIn("provenance", text)
        meta["derived_from"] = [
            {"kind": "dataset", "identifier": "rs2/csac/amber/1_interim/a"},
            {"kind": "external", "url": "https://x.org/b"},
            {"kind": "external", "citation": "Smith 2020"}]
        meta["provenance"] = [{"activity": "clean", "tool": {"name": "clean.py"}}]
        text = "\n".join(deposit.preview_lines(plans, meta, None,
                                               (["f0.csv"], [], [])))
        self.assertIn("derived from: 3 reference(s): rs2/csac/amber/1_interim/a, "
                      "https://x.org/b, ... and 1 more", text)
        self.assertIn("provenance: 1 activity, tool clean.py", text)


class TestPromptOrigin(unittest.TestCase):
    """r8 §3: the two origin questions of a first deposit."""

    def test_lines_become_references_and_a_named_script_an_activity(self):
        meta = {"source_type": "derived"}
        answers = ["rs2/csac/amber/1_interim/csac", "https://x.org/a", "",
                   "y", "1", "cdisaw-parquet", "https://github.com/k/p",
                   "3f2a9c1", "Ran the ingest script unchanged."]
        with mock.patch("builtins.input", side_effect=answers), \
             mock.patch("deposit.say"), mock.patch("deposit.warn") as warned:
            deposit.prompt_origin(meta, _VOCAB)
        self.assertEqual([r["kind"] for r in meta["derived_from"]],
                         ["dataset", "external"])
        act = meta["provenance"][0]
        self.assertEqual(act["activity"], deposit.record.ACTIVITY_KINDS[0])
        self.assertEqual(act["tool"], {"name": "cdisaw-parquet",
                                       "repo": "https://github.com/k/p",
                                       "commit": "3f2a9c1"})
        self.assertEqual(act["description"], "Ran the ingest script unchanged.")
        warned.assert_not_called()

    def test_both_questions_skippable(self):
        meta = {"source_type": "archive"}
        with mock.patch("builtins.input", side_effect=["", "n"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn") as warned:
            deposit.prompt_origin(meta, _VOCAB)
        self.assertNotIn("derived_from", meta)
        self.assertNotIn("provenance", meta)
        warned.assert_not_called()

    def test_derived_with_nothing_named_is_warned_not_refused(self):
        meta = {"source_type": "derived"}
        with mock.patch("builtins.input", side_effect=["", "n"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn") as warned:
            deposit.prompt_origin(meta, _VOCAB)
        self.assertNotIn("derived_from", meta)
        self.assertIn("nothing was named", warned.call_args[0][0])

    def test_activity_codes_come_from_the_vocabulary(self):
        v = dict(_VOCAB, activities=[{"code": "reproject", "label": "Reproject"}])
        meta = {}
        with mock.patch("builtins.input",
                        side_effect=["", "y", "reproject", "gdalwarp", "", "", ""]), \
             mock.patch("deposit.say"):
            deposit.prompt_origin(meta, v)
        self.assertEqual(meta["provenance"][0]["activity"], "reproject")
        self.assertEqual(meta["provenance"][0]["tool"], {"name": "gdalwarp"})


class TestFirstDepositOrigin(unittest.TestCase):
    """The interview asks the origin questions once, on a first deposit,
    and not at all when --provenance supplies the answers."""

    GOOD = " ".join(["word"] * 60)

    def _answers(self):
        # version, coverage x2, subjects, abstract, licence, source type,
        # source detail, creator, steward (origin is mocked out).
        return ["", "1990", "2000", "1", self.GOOD, "", "1",
                "The National Archives, CO 123", "", ""]

    def test_first_deposit_reaches_the_origin_questions(self):
        args = _flagged_args()
        args.provenance_doc = None
        with mock.patch("builtins.input", side_effect=self._answers()), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"), \
             mock.patch("deposit.prompt_origin") as origin:
            meta, existing = deposit.prompt_metadata(args, _VOCAB, None, None, None)
        self.assertIsNone(existing)
        origin.assert_called_once()
        self.assertEqual(meta["source_type"], "archive")

    def test_provenance_file_answers_instead(self):
        args = _flagged_args()
        args.provenance_doc = ([{"activity": "clean", "tool": {"name": "c.py"}}],
                               [{"kind": "dataset",
                                 "identifier": "rs2/csac/amber/1_interim/x"}])
        with mock.patch("builtins.input", side_effect=self._answers()), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"), \
             mock.patch("deposit.prompt_origin") as origin:
            meta, _ = deposit.prompt_metadata(args, _VOCAB, None, None, None)
        origin.assert_not_called()
        self.assertEqual(meta["provenance"][0]["tool"]["name"], "c.py")
        self.assertEqual(meta["derived_from"][0]["identifier"],
                         "rs2/csac/amber/1_interim/x")

    def test_parser_takes_the_flag(self):
        args = deposit.build_parser().parse_args(["a.csv", "--provenance", "p.json"])
        self.assertEqual(args.provenance, "p.json")


class TestConfirmFilenames(unittest.TestCase):
    def test_clean_member_not_prompted(self):
        src = deposit.Source(Path("x"), "2024/tiles/a.tif")
        with mock.patch("builtins.input", side_effect=AssertionError):
            self.assertEqual(deposit.confirm_filenames([src]), {})

    def test_problem_in_nested_segment_offers_correction(self):
        src = deposit.Source(Path("x"), "my folder/bad:name.csv")
        with mock.patch("builtins.input", side_effect=["a"]), \
             mock.patch("deposit.say"):
            renames = deposit.confirm_filenames([src])
        self.assertEqual(renames,
                         {"my folder/bad:name.csv": "my-folder/badname.csv"})

    def test_keep_as_is_records_no_rename(self):
        src = deposit.Source(Path("x"), "bad name.csv")
        with mock.patch("builtins.input", side_effect=["k"]), \
             mock.patch("deposit.say"):
            self.assertEqual(deposit.confirm_filenames([src]), {})

    def test_quit_returns_none(self):
        src = deposit.Source(Path("x"), "bad name.csv")
        with mock.patch("builtins.input", side_effect=["q"]), \
             mock.patch("deposit.say"):
            self.assertIsNone(deposit.confirm_filenames([src]))


class TestPromptPerFileOverrides(unittest.TestCase):
    def test_single_source_never_prompts(self):
        src = deposit.Source(Path("x"), "a.csv")
        with mock.patch("builtins.input", side_effect=AssertionError):
            self.assertEqual(
                deposit.prompt_per_file_overrides([src], dict(_META)), {})

    def test_keyed_on_member_not_local_basename(self):
        # Two sources share a local basename (x.csv) but have different
        # member paths (r7 §1) - overrides must be keyed on the member.
        sources = [deposit.Source(Path("a/x.csv"), "2023/x.csv"),
                  deposit.Source(Path("b/x.csv"), "2024/x.csv")]
        with mock.patch("builtins.input",
                        side_effect=["n", "1990", "1991", "1989", "2025"]), \
             mock.patch("deposit.say"):
            overrides = deposit.prompt_per_file_overrides(sources, dict(_META))
        self.assertIn("2023/x.csv", overrides)
        self.assertNotIn("2024/x.csv", overrides)  # matched the shared dates


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
    return {"path": path, "member": path.name,
            "key": "rs2/csac/green/2_final/sentinel2-imagery/" + path.name,
            "entry_overrides": {}}


class _Args:
    remote = "ceph"
    bucket = "crsw"
    verbose = False
    dry_run = False


RECORD_KEY = ("rs2/csac/green/2_final/sentinel2-imagery/"
             "dataset.sentinel2-imagery.json")


class _Store:
    """Fake bucket: copyto records sizes/labels/order, stat_key answers
    from what was 'uploaded' (or seeded), read_key returns the record."""

    def __init__(self, seed_sizes=None):
        self.sizes = dict(seed_sizes or {})
        self.order = []
        self.labels = {}
        self.texts = {}
        self.fail_on = None
        self.exc = None

    def copyto(self, rclone, lp, remote, bucket, key, show_progress=False,
               headers=None):
        if self.fail_on and self.fail_on in key:
            raise self.exc
        self.order.append(key)
        self.sizes[key] = Path(lp).stat().st_size
        self.labels[key] = dict(headers or {})
        if deposit.keys.is_reserved_name(key.rsplit("/", 1)[-1]):
            self.texts[key] = Path(lp).read_text(encoding="utf-8")

    def stat_key(self, rclone, remote, bucket, key):
        if key in self.sizes:
            return {"Name": key.rsplit("/", 1)[-1], "Size": self.sizes[key]}
        return None

    def read_key(self, rclone, remote, bucket, key):
        if key in self.texts:
            return self.texts[key], None
        return None, "absent"

    def patches(self):
        return (mock.patch("deposit.transfer.copyto", side_effect=self.copyto),
                mock.patch("deposit.transfer.stat_key",
                           side_effect=self.stat_key),
                mock.patch("deposit.transfer.read_key",
                           side_effect=self.read_key))

    def record(self):
        import json
        return json.loads(self.texts[RECORD_KEY])


class TestPerformDeposits(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.f1 = Path(self._tmp.name) / "one.csv"
        self.f1.write_text("1")
        self.f2 = Path(self._tmp.name) / "two.csv"
        self.f2.write_text("2")

    def tearDown(self):
        self._tmp.cleanup()

    def _perform(self, store, files, existing=None, depositor="tester"):
        plans = [_plan_for(f) for f in files]
        entries = deposit.prepare_entries(plans)
        p1, p2, p3 = store.patches()
        with p1, p2, p3, mock.patch("deposit.append_log") as log:
            code = deposit.perform_deposits(
                "rclone", _Args(), plans, dict(_META), entries, existing,
                depositor, _VOCAB)
        return code, log

    def test_members_uploaded_before_record_with_labels(self):
        store = _Store()
        code, log = self._perform(store, [self.f1, self.f2],
                                  depositor="njakeman")
        self.assertEqual(code, 0)
        self.assertEqual(store.order[-1], RECORD_KEY)
        self.assertEqual(len(store.order), 3)
        member_labels = store.labels[
            "rs2/csac/green/2_final/sentinel2-imagery/one.csv"]
        for label in ("x-amz-meta-dataset-uuid", "x-amz-meta-checksum-sha256",
                      "x-amz-meta-sensitivity", "x-amz-meta-depositor"):
            self.assertIn(label, member_labels)
        self.assertEqual(member_labels["x-amz-meta-sensitivity"], "green")
        self.assertEqual(member_labels["x-amz-meta-depositor"], "njakeman")
        self.assertEqual(log.call_count, 3)  # two members + the record

    def test_record_content_first_deposit(self):
        store = _Store()
        code, _ = self._perform(store, [self.f1, self.f2],
                                depositor="njakeman")
        self.assertEqual(code, 0)
        rec = store.record()
        self.assertEqual(rec["schema_version"], deposit.record.SCHEMA_VERSION)
        self.assertEqual(rec["identifier"],
                         "rs2/csac/green/2_final/sentinel2-imagery")
        self.assertEqual(rec["dataset"], "sentinel2-imagery")
        self.assertEqual(len(rec["files"]), 2)
        self.assertEqual(rec["depositors"], ["njakeman"])
        self.assertEqual(rec["created"], rec["modified"])
        self.assertRegex(rec["dataset_uuid"], r"^[0-9a-f-]{36}$")
        self.assertEqual(
            store.labels[RECORD_KEY]["x-amz-meta-dataset-uuid"],
            rec["dataset_uuid"])

    def test_failure_stops_batch_no_record(self):
        store = _Store()
        store.fail_on = "two.csv"
        store.exc = deposit.transfer.TransferError("unreachable", "boom")
        code, _ = self._perform(store, [self.f1, self.f2])
        self.assertNotEqual(code, 0)
        self.assertNotIn(RECORD_KEY, store.texts)

    def test_interrupt_no_record_and_exits_130(self):
        store = _Store()
        store.fail_on = "two.csv"
        store.exc = KeyboardInterrupt()
        code, _ = self._perform(store, [self.f1, self.f2])
        self.assertEqual(code, 130)
        self.assertNotIn(RECORD_KEY, store.texts)

    def test_verification_missing_object_is_error(self):
        store = _Store()
        real = store.copyto

        def copyto_but_lose_members(rclone, lp, remote, bucket, key, **kw):
            real(rclone, lp, remote, bucket, key, **kw)
            if not deposit.keys.is_reserved_name(key.rsplit("/", 1)[-1]):
                del store.sizes[key]  # stored object vanished

        store_patches = store.patches()
        plans = [_plan_for(self.f1)]
        entries = deposit.prepare_entries(plans)
        with mock.patch("deposit.transfer.copyto",
                        side_effect=copyto_but_lose_members), \
             store_patches[1], store_patches[2], \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), plans, dict(_META), entries, None,
                "t", _VOCAB)
        self.assertNotEqual(code, 0)

    def test_size_mismatch_is_error_not_success(self):
        store = _Store()
        real = store.copyto

        def copyto_but_truncate(rclone, lp, remote, bucket, key, **kw):
            real(rclone, lp, remote, bucket, key, **kw)
            if not deposit.keys.is_reserved_name(key.rsplit("/", 1)[-1]):
                store.sizes[key] = 999999

        store_patches = store.patches()
        plans = [_plan_for(self.f1)]
        entries = deposit.prepare_entries(plans)
        with mock.patch("deposit.transfer.copyto",
                        side_effect=copyto_but_truncate), \
             store_patches[1], store_patches[2], \
             mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), plans, dict(_META), entries, None,
                "t", _VOCAB)
        self.assertNotEqual(code, 0)

    def _existing_for(self, files, depositors=("earlier",)):
        entries = deposit.prepare_entries([_plan_for(f) for f in files])
        return {"schema_version": "0.5",
                "dataset_uuid": "8f14e45f-ceea-467f-a34e-9db1c153f0a1",
                "identifier": "rs2/csac/green/2_final/sentinel2-imagery",
                "dataset": "sentinel2-imagery",
                "created": "2026-07-01T00:00:00Z",
                "modified": "2026-07-01T00:00:00Z",
                "temporal": {"start": "1989", "end": "2025"},
                "depositors": list(depositors),
                "files": entries}

    def test_add_to_existing_unions_and_appends_depositor(self):
        existing = self._existing_for([self.f1])
        store = _Store(seed_sizes={
            "rs2/csac/green/2_final/sentinel2-imagery/one.csv":
                self.f1.stat().st_size})
        code, _ = self._perform(store, [self.f2], existing=existing,
                                depositor="njakeman")
        self.assertEqual(code, 0)
        rec = store.record()
        self.assertEqual([e["path"] for e in rec["files"]],
                         ["one.csv", "two.csv"])
        self.assertEqual(rec["dataset_uuid"], existing["dataset_uuid"])
        self.assertEqual(rec["created"], "2026-07-01T00:00:00Z")
        self.assertNotEqual(rec["modified"], rec["created"])
        self.assertEqual(rec["depositors"], ["earlier", "njakeman"])

    def test_rerun_skips_unchanged_and_rewrites_record(self):
        existing = self._existing_for([self.f1, self.f2])
        store = _Store(seed_sizes={
            "rs2/csac/green/2_final/sentinel2-imagery/one.csv":
                self.f1.stat().st_size,
            "rs2/csac/green/2_final/sentinel2-imagery/two.csv":
                self.f2.stat().st_size})
        code, log = self._perform(store, [self.f1, self.f2],
                                  existing=existing)
        self.assertEqual(code, 0)
        # Metadata-only: exactly one object touched - the record (r5 Q6).
        self.assertEqual(store.order, [RECORD_KEY])
        self.assertEqual(log.call_count, 1)

    def test_per_file_coverage_widens_envelope(self):
        store = _Store()
        plans = [_plan_for(self.f1)]
        plans[0]["entry_overrides"] = {"coverage_start": "1960",
                                       "coverage_end": "2026-01-01"}
        entries = deposit.prepare_entries(plans)
        p1, p2, p3 = store.patches()
        with p1, p2, p3, mock.patch("deposit.append_log"):
            code = deposit.perform_deposits(
                "rclone", _Args(), plans, dict(_META), entries, None,
                "t", _VOCAB)
        self.assertEqual(code, 0)
        rec = store.record()
        self.assertEqual(rec["temporal"]["start"], "1960")
        self.assertEqual(rec["temporal"]["end"], "2026-01-01")


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

    def test_reserved_name_formats(self):
        msg = deposit.MESSAGES["reserved_name"].format(name="dataset.meta.json")
        self.assertIn("dataset.meta.json", msg)

    def test_record_invalid_says_nothing_was_written(self):
        self.assertIn("NOT written", deposit.MESSAGES["record_invalid"])

    def test_restricted_first_says_before_not_after(self):
        self.assertIn("BEFORE the", deposit.MESSAGES["restricted_first"])


class TestReservedNameRefusal(unittest.TestCase):
    def test_depositing_the_record_name_fails_before_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dataset.meta.json"
            p.write_text("{}")
            said = []
            with mock.patch("deposit.say", side_effect=said.append), \
                 mock.patch("deposit.warn"), \
                 mock.patch("deposit.fail", side_effect=lambda m, c=1: c) as f, \
                 mock.patch("deposit.load_config",
                            return_value={"remote": "ceph", "bucket": "crsw"}):
                code = deposit.main([str(p), "--remote", "ceph",
                                     "--bucket", "crsw"])
        self.assertNotEqual(code, 0)
        self.assertIn("reserved", f.call_args[0][0])


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
        # Trailing "n": declines the restricted-access guard's question.
        result, said, _, _ = self._run(["csac", "x"],
                                       ["n", "CSAC Data", "y", "n"])
        self.assertEqual(result, "csac-data")
        self.assertIn("  -> normalised to: csac-data", said)

    def test_empty_listing_notes_and_creates_first(self):
        result, said, _, _ = self._run([], ["first-project", "y", "n"])
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
            ["csac"], ["n", "csacs", "n", "peacekeeping", "y", "n"])
        self.assertEqual(result, "peacekeeping")
        self.assertTrue(any("csac" in w for w in warned))

    def test_restricted_access_declined_aborts(self):
        # Creation confirmed, but access needs restricting beyond the
        # strand -> matrix-first, nothing deposited (r6 §8).
        with mock.patch("builtins.input",
                        side_effect=["n", "new-project", "y", "y"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            with self.assertRaises(ValueError) as ctx:
                deposit.prompt_project("rs2", ["csac"])
        self.assertIn("restrictions must be in place", str(ctx.exception))

    def test_legacy_invalid_name_reprompts(self):
        result, said, _, _ = self._run(["CSAC", "csac-data"], ["1", "2"])
        self.assertEqual(result, "csac-data")
        self.assertTrue(any("predates the naming rule" in s for s in said))


class TestPromptDataset(unittest.TestCase):
    """r6 §1: the dataset picker mirrors the project picker exactly, one
    level down - same functions, kind="dataset", not a second
    implementation. Container is the full state prefix, not a strand."""

    CONTAINER = "rs3/kilns/green/2_final"

    def _run(self, listing, inputs):
        said, warned = [], []
        with mock.patch("builtins.input", side_effect=inputs), \
             mock.patch("deposit.say", side_effect=said.append), \
             mock.patch("deposit.warn", side_effect=warned.append):
            result = deposit.prompt_project(self.CONTAINER, listing,
                                            kind="dataset")
        return result, said, warned

    def test_select_existing_by_number(self):
        result, said, _ = self._run(
            ["sentinel2-imagery", "training-labels"], ["1"])
        self.assertEqual(result, "sentinel2-imagery")
        self.assertTrue(any(
            "Datasets in %s" % self.CONTAINER in s for s in said))
        self.assertTrue(any("[n] new dataset" in s for s in said))

    def test_empty_listing_notes_and_creates_first(self):
        result, said, _ = self._run([], ["first-dataset", "y", "n"])
        self.assertEqual(result, "first-dataset")
        self.assertTrue(any(
            "No datasets in %s yet" % self.CONTAINER in s for s in said))

    def test_none_listing_falls_back_honestly(self):
        result, said, _ = self._run(None, ["solo-dataset", "y"])
        self.assertEqual(result, "solo-dataset")
        self.assertTrue(any("Couldn't list existing datasets" in s
                            for s in said))

    def test_near_match_warns(self):
        # r6 §6: near-match dataset name (sentinel2 vs sentinel2-imagery).
        result, _, warned = self._run(
            ["sentinel2-imagery"], ["n", "sentinel2", "y", "n"])
        self.assertEqual(result, "sentinel2")
        self.assertTrue(any("sentinel2-imagery" in w for w in warned))

    def test_new_dataset_confirmation_wording(self):
        with mock.patch("builtins.input",
                        side_effect=["n", "labels", "y", "n"]) as inp, \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            deposit.prompt_project(self.CONTAINER, ["existing"], kind="dataset")
        prompts = [str(c.args[0]) for c in inp.call_args_list]
        self.assertTrue(any("Create new dataset" in p for p in prompts))

    def test_restricted_access_confirmed_aborts_with_no_deposit(self):
        with mock.patch("builtins.input",
                        side_effect=["n", "restricted-set", "y", "y"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            with self.assertRaises(ValueError) as ctx:
                deposit.prompt_project(self.CONTAINER, ["existing"],
                                       kind="dataset")
        self.assertIn("BEFORE the", str(ctx.exception))


def _flagged_args():
    return deposit.build_parser().parse_args(
        ["a.csv", "--strand", "rs2", "--project", "csac",
         "--dataset", "sentinel2-imagery", "--state", "2_final",
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
        self.assertEqual(probed,
                         ["rs2/csac/green/2_final/sentinel2-imagery"])
        self.assertEqual(ctx.exception.kind, "permission")
        self.assertEqual(ctx.exception.detail,
                         "rs2/csac/green/2_final/sentinel2-imagery")

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


class TestExistingRecordFlow(unittest.TestCase):
    VOCAB = {"vocabulary_version": "2026-07-23",
             "domains": [{"code": "quant", "label": "Quantitative",
                          "steward": "Tester"}],
             "facets": {"themes": ["armed-conflict"]}}
    RECORD_KEY = ("rs2/csac/green/2_final/sentinel2-imagery/"
                 "dataset.sentinel2-imagery.json")

    def _existing(self, **overrides):
        rec = {"schema_version": "0.5",
               "dataset_uuid": "8f14e45f-ceea-467f-a34e-9db1c153f0a1",
               "identifier": "rs2/csac/green/2_final/sentinel2-imagery",
               "strand": "rs2", "project": "csac",
               "dataset": "sentinel2-imagery", "sensitivity": "green",
               "state": "2_final", "domain": "quant", "version": "3-0",
               "abstract": "kept " * 120,
               "subject": ["armed-conflict"],
               "temporal": {"start": "1989", "end": "2025"},
               "creator": "CSAC team", "license": "CC-BY-4.0",
               "source_type": "archive", "source_detail": "CSAC project",
               "steward": "Kevin Fahey",
               "created": "2026-07-01T00:00:00Z",
               "modified": "2026-07-01T00:00:00Z",
               "files": [{"path": "old.csv", "checksum_sha256": "ab" * 32,
                          "bytes": 1}]}
        rec.update(overrides)
        return rec

    def _fetcher(self, result):
        fetched = []

        def fetch(key):
            fetched.append(key)
            return result
        fetch.calls = fetched
        return fetch

    def test_existing_record_answers_the_interview(self):
        import json
        fetch = self._fetcher((json.dumps(self._existing()), None))
        said = []
        # Remaining prompts only: version (Enter=3-0), coverage x2
        # (Enter keeps), update-abstract? (n).
        with mock.patch("builtins.input",
                        side_effect=["", "", "", "n"]), \
             mock.patch("deposit.say", side_effect=said.append), \
             mock.patch("deposit.warn"):
            meta, existing = deposit.prompt_metadata(
                _flagged_args(), self.VOCAB, None, None, fetch)
        self.assertEqual(fetch.calls, [self.RECORD_KEY])
        self.assertIsNotNone(existing)
        self.assertEqual(meta["version"], "3-0")
        self.assertEqual(meta["abstract"], self._existing()["abstract"])
        self.assertEqual(meta["subject"], ["armed-conflict"])
        self.assertEqual(meta["creator"], "CSAC team")
        self.assertEqual(meta["steward"], "Kevin Fahey")
        self.assertEqual(meta["source_type"], "archive")
        self.assertTrue(any(
            "Existing dataset found: sentinel2-imagery 3-0" in s
            for s in said))

    def test_existing_derived_from_is_left_to_the_merge_rule(self):
        import json
        fetch = self._fetcher((json.dumps(self._existing(
            derived_from="rs2/csac/amber/1_interim/csac")), None))
        with mock.patch("builtins.input", side_effect=["", "", "", "n"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            meta, existing = deposit.prompt_metadata(
                _flagged_args(), self.VOCAB, None, None, fetch)
        # Not copied into meta (r8 §3); assemble_record keeps the
        # existing list, already upgraded to a reference on read.
        self.assertNotIn("derived_from", meta)
        self.assertEqual(existing["derived_from"][0]["kind"], "dataset")

    def test_absent_record_is_a_first_deposit(self):
        fetch = self._fetcher((None, "absent"))

        class PastFetch(Exception):
            pass

        with mock.patch("builtins.input", side_effect=PastFetch), \
             mock.patch("deposit.say"):
            with self.assertRaises(PastFetch):
                deposit.prompt_metadata(_flagged_args(), self.VOCAB,
                                        None, None, fetch)
        self.assertEqual(fetch.calls, [self.RECORD_KEY])

    def test_unparseable_record_raises_with_key(self):
        fetch = self._fetcher(("{not json", None))
        with mock.patch("builtins.input", side_effect=AssertionError), \
             mock.patch("deposit.say"):
            with self.assertRaises(deposit.record.RecordParseError) as ctx:
                deposit.prompt_metadata(_flagged_args(), self.VOCAB,
                                        None, None, fetch)
        self.assertEqual(ctx.exception.key, self.RECORD_KEY)

    def test_fetch_error_is_fatal_not_absence(self):
        fetch = self._fetcher((None, "permission"))
        with mock.patch("builtins.input", side_effect=AssertionError), \
             mock.patch("deposit.say"):
            with self.assertRaises(deposit.transfer.TransferError) as ctx:
                deposit.prompt_metadata(_flagged_args(), self.VOCAB,
                                        None, None, fetch)
        self.assertEqual(ctx.exception.kind, "permission")
        self.assertEqual(ctx.exception.detail, self.RECORD_KEY)

    def test_short_abstract_gated_then_reprompted(self):
        import json
        fetch = self._fetcher((json.dumps(self._existing()), None))
        good = " ".join(["word"] * 60)
        # version, cov x2, update-abstract=y, short abstract,
        # continue-anyway=n -> re-prompt, good abstract.
        with mock.patch("builtins.input",
                        side_effect=["", "", "", "y", "tiny", "n", good]), \
             mock.patch("deposit.say"), \
             mock.patch("deposit.warn") as warned:
            meta, _ = deposit.prompt_metadata(
                _flagged_args(), self.VOCAB, None, None, fetch)
        self.assertEqual(meta["abstract"], good)
        self.assertIn("1 words", warned.call_args[0][0])

    def test_short_abstract_accepted_on_continue(self):
        # r6 §0: lenience is deliberate for the testing phase - warned,
        # gated behind y/N, never refused.
        import json
        fetch = self._fetcher((json.dumps(self._existing()), None))
        with mock.patch("builtins.input",
                        side_effect=["", "", "", "y", "2", "y"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            meta, _ = deposit.prompt_metadata(
                _flagged_args(), self.VOCAB, None, None, fetch)
        self.assertEqual(meta["abstract"], "2")

    def test_fetch_error_downgraded_under_dry_run(self):
        args = deposit.build_parser().parse_args(
            ["a.csv", "--strand", "rs2", "--project", "csac",
             "--dataset", "sentinel2-imagery",
             "--state", "2_final", "--sensitivity", "green",
             "--domain", "quant", "--dry-run"])
        fetch = self._fetcher((None, "permission"))
        warned = []

        class PastFetch(Exception):
            pass

        with mock.patch("builtins.input", side_effect=PastFetch), \
             mock.patch("deposit.say"), \
             mock.patch("deposit.warn", side_effect=warned.append):
            with self.assertRaises(PastFetch):
                deposit.prompt_metadata(args, self.VOCAB, None, None, fetch)
        self.assertTrue(any("first deposit" in w for w in warned))


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

    def test_new_project_confirms_then_asks_about_restriction(self):
        # Creation confirmed ("y"), then the matrix-first restricted-
        # access question ("n" = no restriction needed) - two prompts,
        # not one (r6 §8).
        with mock.patch("builtins.input", side_effect=["y", "n"]) as inp:
            deposit.check_project_flag("newproj", "rs2", ["csac"], False)
        self.assertEqual(inp.call_count, 2)

    def test_decline_raises_valueerror(self):
        with mock.patch("builtins.input", side_effect=["n"]):
            with self.assertRaises(ValueError):
                deposit.check_project_flag("newproj", "rs2", ["csac"], False)

    def test_restricted_access_yes_aborts(self):
        with mock.patch("builtins.input", side_effect=["y", "y"]):
            with self.assertRaises(ValueError) as ctx:
                deposit.check_project_flag("newproj", "rs2", ["csac"], False)
        self.assertIn("restrictions must be in place", str(ctx.exception))

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


class TestOfferToSaveSettings(unittest.TestCase):
    def _run(self, flagged_remote, flagged_bucket, cfg, remote, bucket,
            already_set_up=False, dry_run=False, answer="y"):
        calls = []
        with mock.patch("deposit.interactive", return_value=True), \
             mock.patch("builtins.input", return_value=answer), \
             mock.patch("deposit.save_config",
                        side_effect=lambda c, path=None: calls.append(c)), \
             mock.patch("deposit.say"):
            deposit.offer_to_save_settings(
                flagged_remote, flagged_bucket, cfg, remote, bucket,
                already_set_up, dry_run)
        return calls

    def test_already_set_up_stays_silent(self):
        with mock.patch("builtins.input", side_effect=AssertionError):
            deposit.offer_to_save_settings("k1", None, {}, "k1", "crsw",
                                           True, False)

    def test_no_flags_given_stays_silent(self):
        with mock.patch("builtins.input", side_effect=AssertionError):
            deposit.offer_to_save_settings(
                None, None, {"remote": "ceph", "bucket": "crsw"},
                "ceph", "crsw", False, False)

    def test_dry_run_stays_silent(self):
        with mock.patch("builtins.input", side_effect=AssertionError):
            deposit.offer_to_save_settings("k1", None, {}, "k1", "crsw",
                                           False, True)

    def test_non_tty_stays_silent(self):
        with mock.patch("deposit.interactive", return_value=False), \
             mock.patch("builtins.input", side_effect=AssertionError):
            deposit.offer_to_save_settings("k1", None, {}, "k1", "crsw",
                                           False, False)

    def test_matching_saved_value_stays_silent(self):
        with mock.patch("deposit.interactive", return_value=True), \
             mock.patch("builtins.input", side_effect=AssertionError):
            deposit.offer_to_save_settings(
                "ceph", "crsw", {"remote": "ceph", "bucket": "crsw"},
                "ceph", "crsw", False, False)

    def test_differing_value_prompts_and_saves_on_yes(self):
        calls = self._run("k1", None, {"remote": "ceph", "bucket": "crsw"},
                          "k1", "crsw", answer="y")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["remote"], "k1")
        self.assertEqual(calls[0]["bucket"], "crsw")

    def test_differing_value_declines_on_no(self):
        calls = self._run("k1", None, {"remote": "ceph", "bucket": "crsw"},
                          "k1", "crsw", answer="n")
        self.assertEqual(calls, [])


class TestFolderDepositIntegration(unittest.TestCase):
    """The pieces of r7 §1 exercised together, from a real folder on
    disk through to the preview and the completion-check invariant -
    without driving the full interactive interview in main()."""

    def test_folder_argument_through_to_preview(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "survey-2024"
            (base / "2024" / "tiles").mkdir(parents=True)
            (base / "2024" / "tiles" / "a.tif").write_text("x")
            (base / "readme.md").write_text("y")
            sources, problems, notes = deposit.resolve_files([str(base)])
            self.assertEqual(problems, [])
            plans = deposit.plan_deposits(sources, dict(_META), {})
            entries = deposit.prepare_entries(plans)
            classification = deposit.classify_members(entries, None)
            text = "\n".join(deposit.preview_lines(
                plans, dict(_META), None, classification, limit=10))

        self.assertEqual(sorted(e["path"] for e in entries),
                         ["2024/tiles/a.tif", "readme.md"])
        self.assertIn("2024/tiles/a.tif", text)
        self.assertIn("readme.md", text)

        prefix = deposit.keys.dataset_prefix(
            _META["strand"], _META["project"], _META["sensitivity"],
            _META["state"], _META["dataset"])
        for plan, entry in zip(plans, entries):
            self.assertEqual(plan["key"], prefix + "/" + entry["path"])


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


class TestStaleSubjectsDefault(TestExistingRecordFlow):
    """r9 step D: an existing record whose subjects the vocabulary has
    since changed gets the mapped terms as the default."""

    def _vocab(self):
        from crsw_deposit import authority
        base = {"vocabulary_version": "2026-07-23",
                "domains": [{"code": "quant", "label": "Quantitative",
                             "steward": "Tester"}],
                "terms": [{"slug": "armed-conflict", "facet": "themes",
                           "label": "Armed conflict", "status": "current",
                           "since": "2026-07-23"},
                          {"slug": "civil-war", "facet": "themes",
                           "label": "Civil war", "status": "current",
                           "since": "2026-07-23"},
                          {"slug": "survey", "facet": "methods",
                           "label": "Survey", "status": "current",
                           "since": "2026-07-23"}],
                "changes": []}
        base["facets"] = authority.facets_from_terms(base)
        return base

    def test_enter_keeps_the_mapped_default(self):
        import json
        from crsw_deposit import authority
        v = authority.apply_change(self._vocab(), {
            "kind": "merge", "from": ["civil-war"], "to": ["armed-conflict"],
            "date": "2026-10-01", "by": "k1"})
        fetch = self._fetcher((json.dumps(self._existing(
            subject=["civil-war", "survey"])), None))
        said = []
        # version, coverage x2, subjects (Enter = mapped default), abstract n
        with mock.patch("builtins.input", side_effect=["", "", "", "", "n"]), \
             mock.patch("deposit.say", side_effect=said.append), \
             mock.patch("deposit.warn") as warned:
            meta, _ = deposit.prompt_metadata(_flagged_args(), v, None, None, fetch)
        self.assertEqual(meta["subject"], ["armed-conflict", "survey"])
        self.assertTrue(any("civil-war -> armed-conflict" in s for s in said), said)
        warned.assert_not_called()

    def test_typed_answer_overrides_the_default(self):
        import json
        from crsw_deposit import authority
        v = authority.apply_change(self._vocab(), {
            "kind": "merge", "from": ["civil-war"], "to": ["armed-conflict"],
            "date": "2026-10-01", "by": "k1"})
        fetch = self._fetcher((json.dumps(self._existing(subject=["civil-war"])), None))
        with mock.patch("builtins.input", side_effect=["", "", "", "survey", "n"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn"):
            meta, _ = deposit.prompt_metadata(_flagged_args(), v, None, None, fetch)
        self.assertEqual(meta["subject"], ["survey"])

    def test_split_is_offered_with_a_check_note(self):
        import json
        from crsw_deposit import authority
        v = self._vocab()
        v = authority.apply_change(v, {"kind": "add", "to": ["survey-online"],
                                       "facet": "methods", "label": "Online",
                                       "date": "2026-10-01", "by": "k1"})
        v = authority.apply_change(v, {"kind": "add", "to": ["survey-household"],
                                       "facet": "methods", "label": "Household",
                                       "date": "2026-10-01", "by": "k1"})
        v = authority.apply_change(v, {"kind": "split", "from": ["survey"],
                                       "to": ["survey-online", "survey-household"],
                                       "date": "2026-10-02", "by": "k1"})
        fetch = self._fetcher((json.dumps(self._existing(subject=["survey"])), None))
        said = []
        with mock.patch("builtins.input", side_effect=["", "", "", "", "n"]), \
             mock.patch("deposit.say", side_effect=said.append), \
             mock.patch("deposit.warn"):
            meta, _ = deposit.prompt_metadata(_flagged_args(), v, None, None, fetch)
        self.assertEqual(meta["subject"], ["survey-online", "survey-household"])
        self.assertTrue(any("a split - check which apply" in s for s in said), said)

    def test_nothing_maps_falls_back_to_the_old_warning(self):
        import json
        from crsw_deposit import authority
        v = authority.apply_change(self._vocab(), {
            "kind": "retire", "from": ["civil-war"], "date": "2026-10-01", "by": "k1"})
        fetch = self._fetcher((json.dumps(self._existing(subject=["civil-war"])), None))
        with mock.patch("builtins.input", side_effect=["", "", "", "1", "n"]), \
             mock.patch("deposit.say"), mock.patch("deposit.warn") as warned:
            meta, _ = deposit.prompt_metadata(_flagged_args(), v, None, None, fetch)
        self.assertEqual(meta["subject"], ["armed-conflict"])
        self.assertTrue(any("choose again" in str(c) for c in warned.call_args_list))


class TestHierarchyListing(unittest.TestCase):
    """r9 §1.8: the listing indents narrower terms; a flat vocabulary
    prints byte-identically."""

    def _vocab(self, with_child):
        from crsw_deposit import authority
        v = {"vocabulary_version": "2026-07-23",
             "terms": [{"slug": "osint", "facet": "methods", "label": "OSINT",
                        "status": "current", "since": "2026-07-23"},
                       {"slug": "survey", "facet": "methods", "label": "Survey",
                        "status": "current", "since": "2026-07-23"}],
             "changes": []}
        if with_child:
            v["terms"].insert(1, {"slug": "osint-social", "facet": "methods",
                                  "label": "Social", "status": "current",
                                  "since": "2026-07-23", "broader": "osint"})
        v["facets"] = authority.facets_from_terms(v)
        return v

    def test_flat_vocabulary_lists_as_before(self):
        v = self._vocab(with_child=False)
        entries = deposit.subject_entries(v)
        self.assertEqual(deposit.subject_depths(v), {"osint": 0, "survey": 0})
        self.assertEqual(deposit.subject_listing_lines(entries, 80),
                         deposit.subject_listing_lines(entries, 80,
                                                       deposit.subject_depths(v)))
        # An empty cache dir, so the machine's real cache (which may hold
        # a vocabulary with children) cannot leak in.
        with tempfile.TemporaryDirectory() as d:
            bundled = deposit.vocab.load_vocabulary(
                cache=Path(d), opener=lambda u, t: (_ for _ in ()).throw(OSError()))[0]
        self.assertTrue(all(d == 0 for d in deposit.subject_depths(bundled).values()))

    def test_child_is_indented_and_numbered_in_order(self):
        v = self._vocab(with_child=True)
        entries = deposit.subject_entries(v)
        self.assertEqual([t for _n, t, _f in entries], ["osint", "osint-social", "survey"])
        lines = deposit.subject_listing_lines(entries, 80, deposit.subject_depths(v))
        body = [l for l in lines if "[" in l]
        self.assertIn("  osint-social", body[1])
        self.assertNotIn("  osint ", body[0].replace("[1] osint", "[1]osint"))
        resolved, _ = deposit.resolve_subjects("2", entries)
        self.assertEqual(resolved, ["osint-social"])
