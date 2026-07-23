import io
import json
import tempfile
import unittest
from pathlib import Path

import vocab

GOOD = {"vocabulary_version": "2026-07-23",
        "facets": {"practices": ["forced-labour"], "contexts": ["armed-conflict"]}}


def opener_returning(payload_bytes):
    def opener(url, timeout):
        return io.BytesIO(payload_bytes)
    return opener


def opener_failing(url, timeout):
    raise OSError("network down")


class TestLoadVocabulary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.bundled = Path(self._tmp.name) / "vocab.json"
        self.bundled.write_text(json.dumps(GOOD), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_remote_success_used_and_cached(self):
        remote = dict(GOOD, vocabulary_version="2026-08-01")
        v, source = vocab.load_vocabulary(
            cache=self.cache, bundled=self.bundled,
            opener=opener_returning(json.dumps(remote).encode("utf-8")))
        self.assertEqual(source, "remote")
        self.assertEqual(v["vocabulary_version"], "2026-08-01")
        cached = json.loads((self.cache / "vocab.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["vocabulary_version"], "2026-08-01")

    def test_fetch_failure_falls_back_to_cache(self):
        self.cache.mkdir(parents=True)
        cached = dict(GOOD, vocabulary_version="2026-07-30")
        (self.cache / "vocab.json").write_text(json.dumps(cached), encoding="utf-8")
        v, source = vocab.load_vocabulary(
            cache=self.cache, bundled=self.bundled, opener=opener_failing)
        self.assertEqual(source, "cache")
        self.assertEqual(v["vocabulary_version"], "2026-07-30")

    def test_fetch_failure_no_cache_falls_back_to_bundled(self):
        v, source = vocab.load_vocabulary(
            cache=self.cache, bundled=self.bundled, opener=opener_failing)
        self.assertEqual(source, "bundled")
        self.assertEqual(v, GOOD)

    def test_malformed_remote_json_falls_back(self):
        v, source = vocab.load_vocabulary(
            cache=self.cache, bundled=self.bundled,
            opener=opener_returning(b"<html>error page</html>"))
        self.assertEqual(source, "bundled")

    def test_remote_json_without_facets_falls_back(self):
        v, source = vocab.load_vocabulary(
            cache=self.cache, bundled=self.bundled,
            opener=opener_returning(b'{"not_facets": true}'))
        self.assertEqual(source, "bundled")


class TestHelpers(unittest.TestCase):
    def test_all_terms_unions_facets(self):
        self.assertEqual(vocab.all_terms(GOOD), {"forced-labour", "armed-conflict"})

    def test_bundled_vocab_path_exists_in_repo(self):
        self.assertTrue(vocab.bundled_vocab_path().is_file())

    def test_bundled_vocab_is_valid(self):
        data = json.loads(vocab.bundled_vocab_path().read_text(encoding="utf-8"))
        self.assertIn("facets", data)
        self.assertIn("vocabulary_version", data)

    def test_cache_dir_is_named_crsw_deposit(self):
        self.assertEqual(vocab.cache_dir().name, "crsw-deposit")


if __name__ == "__main__":
    unittest.main()
