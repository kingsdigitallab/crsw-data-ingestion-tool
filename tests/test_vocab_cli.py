"""r9 step B: crsw-vocab edits an authority file from the command line.
Runs the module as a subprocess on a copy of the bundled file."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crsw_deposit import authority, vocab

ROOT = Path(__file__).resolve().parent.parent


class TestVocabCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "vocab.json"
        shutil.copy(str(vocab.bundled_vocab_path()), str(self.file))

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, expect=0):
        cmd = [sys.executable, "-m", "crsw_deposit.vocab_cli",
               "--file", str(self.file)] + list(args)
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(proc.returncode, expect,
                         "\n".join([" ".join(args), proc.stdout, proc.stderr]))
        return proc.stdout + proc.stderr

    def doc(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def common(self):
        return ["--by", "k1078591", "--date", "2026-10-01", "--note", "issue #1"]

    def test_validate_bundled(self):
        out = self.run_cli("validate")
        self.assertIn("valid", out)
        self.assertIn("34 current term", out)

    def test_validate_reports_problems(self):
        d = self.doc()
        d["terms"][0]["status"] = "gone"
        self.file.write_text(json.dumps(d), encoding="utf-8")
        out = self.run_cli("validate", expect=1)
        self.assertIn("current or retired", out)

    def test_merge_diff_touches_only_the_term_and_the_change_list(self):
        before = self.file.read_text(encoding="utf-8").splitlines()
        out = self.run_cli("merge", "debt-bondage", "--into", "forced-labour",
                           *self.common())
        self.assertIn("merged debt-bondage into forced-labour", out)
        after = self.file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(authority.validate_authority(self.doc()), [])
        changed = [l for l in after if l not in before]
        # the retired term, the change entry and the now-open changes
        # bracket, the version and the facet line: nothing else moves
        self.assertEqual(len(changed), 5, changed)
        self.assertIn('  "changes": [', changed)
        self.assertTrue(any('"debt-bondage"' in l and '"retired"' in l for l in changed))
        self.assertTrue(any('"kind": "merge"' in l for l in changed))
        self.assertTrue(any('"vocabulary_version": "2026-10-01"' in l for l in changed))
        self.assertTrue(any(l.strip().startswith('"practices"') for l in changed))
        # "changes": [] becomes three lines: bracket, entry, bracket
        self.assertEqual(len(before), len(after) - 2)

    def test_split_creates_new_terms_then_splits(self):
        self.run_cli("split", "survey", "--into", "survey-household=Household survey",
                     "survey-online=Online survey", *self.common())
        d = self.doc()
        self.assertEqual([c["kind"] for c in d["changes"]], ["add", "add", "split"])
        by = authority.terms_by_slug(d)
        self.assertEqual(by["survey"]["replaced_by"], ["survey-household", "survey-online"])
        self.assertEqual(by["survey-online"]["label"], "Online survey")
        self.assertEqual(authority.map_subjects(["survey"], d)[0],
                         ["survey-household", "survey-online"])

    def test_split_into_unknown_bare_slug_is_refused_and_file_untouched(self):
        before = self.file.read_text(encoding="utf-8")
        out = self.run_cli("split", "survey", "--into", "a", "b", *self.common(),
                           expect=1)
        self.assertIn("give it a label", out)
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)

    def test_add_rename_move_retire_tree(self):
        self.run_cli("add", "osint-social", "--facet", "methods",
                     "--label", "OSINT: social media", "--broader", "osint",
                     *self.common())
        out = self.run_cli("tree", "methods")
        self.assertIn("  osint  ", out)
        self.assertIn("    osint-social  OSINT: social media", out)
        self.run_cli("rename", "osint", "open-source", "--label", "Open source",
                     "--by", "k1", "--date", "2026-10-02")
        d = self.doc()
        self.assertEqual(authority.terms_by_slug(d)["osint-social"]["broader"],
                         "open-source")
        out = self.run_cli("retire", "open-source", "--by", "k1",
                           "--date", "2026-10-03", expect=1)
        self.assertIn("move them first", out)
        self.run_cli("move", "osint-social", "--top", "--by", "k1", "--date", "2026-10-03")
        self.run_cli("retire", "open-source", "--by", "k1", "--date", "2026-10-03")
        d = self.doc()
        self.assertEqual(authority.terms_by_slug(d)["open-source"]["status"], "retired")
        self.assertNotIn("broader", authority.terms_by_slug(d)["osint-social"])
        self.assertEqual(authority.validate_authority(d), [])
        self.assertEqual(d["vocabulary_version"], "2026-10-03")

    def test_move_under_parent_and_earlier_date_refused(self):
        self.run_cli("add", "osint-social", "--facet", "methods", "--label", "x",
                     *self.common())
        self.run_cli("move", "osint-social", "--under", "osint", "--by", "k1",
                     "--date", "2026-10-02")
        self.assertEqual(authority.terms_by_slug(self.doc())["osint-social"]["broader"],
                         "osint")
        out = self.run_cli("retire", "maritime", "--by", "k1", "--date", "2026-09-01",
                           expect=1)
        self.assertIn("before the change above it", out)

    def test_flat_file_is_refused(self):
        self.file.write_text(json.dumps({"vocabulary_version": "2026-01-01",
                                         "facets": {"a": ["b"]}}), encoding="utf-8")
        out = self.run_cli("validate", expect=1)
        self.assertIn("not an authority file", out)

    def test_dump_round_trips_the_bundled_file_byte_for_byte(self):
        text = vocab.bundled_vocab_path().read_text(encoding="utf-8")
        self.assertEqual(authority.dump_authority(json.loads(text)), text)


if __name__ == "__main__":
    unittest.main()
