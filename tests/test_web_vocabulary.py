"""The web service's vocabulary refreshes while it runs; the form shows
narrower terms indented."""
import copy
import json
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    from crsw_web.vocabulary import VocabularyCache
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import authority, vocab

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t",
                s3_secret_key="t", s3_bucket="crsw", staging_prefix="staging/_test")


def newer():
    return authority.apply_change(VOCAB, {
        "kind": "add", "to": ["interstate-war"], "facet": "contexts",
        "label": "Interstate war", "broader": "armed-conflict",
        "date": "2026-10-01", "by": "k1"})


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestVocabularyCache(unittest.TestCase):
    def make(self, results, refresh=600):
        calls = []

        def loader():
            calls.append(1)
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        clock = FakeClock()
        cache = VocabularyCache(copy.deepcopy(VOCAB), "bundled",
                                refresh_seconds=refresh, loader=loader, clock=clock)
        return cache, clock, calls

    def test_no_fetch_before_the_interval(self):
        cache, clock, calls = self.make([(newer(), "remote")])
        clock.now += 599
        self.assertEqual(cache.current()["vocabulary_version"], "2026-07-23")
        self.assertEqual(calls, [])

    def test_newer_remote_file_replaces_the_copy_after_the_interval(self):
        cache, clock, calls = self.make([(newer(), "remote")])
        clock.now += 600
        doc = cache.current()
        self.assertEqual(doc["vocabulary_version"], "2026-10-01")
        self.assertIn("interstate-war", cache.terms)
        self.assertEqual(cache.source, "remote")
        self.assertEqual(calls, [1])
        # not fetched again until the next interval
        cache.current()
        self.assertEqual(calls, [1])

    def test_failed_fetch_keeps_the_copy_and_waits_an_interval(self):
        cache, clock, calls = self.make([OSError("down"), (newer(), "remote")])
        clock.now += 600
        self.assertEqual(cache.current()["vocabulary_version"], "2026-07-23")
        self.assertEqual(calls, [1])
        clock.now += 1
        cache.current()
        self.assertEqual(calls, [1])     # backed off
        clock.now += 600
        self.assertEqual(cache.current()["vocabulary_version"], "2026-10-01")

    def test_bundled_result_is_not_an_upgrade(self):
        cache, clock, calls = self.make([(newer(), "bundled")])
        clock.now += 600
        self.assertEqual(cache.current()["vocabulary_version"], "2026-07-23")

    def test_invalid_remote_file_is_ignored(self):
        bad = newer()
        bad["terms"][0]["status"] = "gone"
        cache, clock, calls = self.make([(bad, "remote")])
        clock.now += 600
        self.assertEqual(cache.current()["vocabulary_version"], "2026-07-23")

    def test_zero_interval_never_refreshes(self):
        cache, clock, calls = self.make([(newer(), "remote")], refresh=0)
        clock.now += 10 ** 6
        cache.current()
        self.assertEqual(calls, [])


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestFormAndEndpoint(unittest.TestCase):
    def client(self, doc):
        return TestClient(create_app(Settings(**SETTINGS), s3_client=object(),
                                     vocab_dict=doc))

    def test_vocabulary_endpoint_reports_source_and_time(self):
        r = self.client(VOCAB).get("/vocabulary").json()
        self.assertEqual(r["vocabulary_version"], "2026-07-23")
        self.assertEqual(r["vocabulary_source"], "injected")
        self.assertRegex(r["vocabulary_loaded"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(list(r["facets"]), ["practices", "themes", "methods", "contexts"])

    def test_form_indents_a_narrower_term(self):
        html = self.client(newer()).get("/").text
        self.assertIn('class="depth-1"><input type="checkbox" name="subject" '
                      'value="interstate-war">', html)
        self.assertIn('class="depth-0"><input type="checkbox" name="subject" '
                      'value="armed-conflict">', html)
        self.assertIn("vocabulary 2026-10-01", html)

    def test_flat_vocabulary_is_all_depth_zero(self):
        flat = {"vocabulary_version": "x",
                "facets": {"practices": ["forced-labour"], "contexts": ["armed-conflict"]}}
        html = self.client(flat).get("/").text
        self.assertNotIn("depth-1", html)
        self.assertEqual(html.count('class="depth-0"'), 2)

    def test_refreshed_vocabulary_is_used_for_validation(self):
        # Swap the cache's copy as a refresh would, then a new term is
        # accepted by the metadata check.
        from crsw_web.app import create_app as make
        app = make(Settings(**SETTINGS), s3_client=object(), vocab_dict=VOCAB)
        cache = app.state.vocab_cache
        cache._loader = lambda: (newer(), "remote")
        cache.refresh_seconds = 1
        cache._loaded_at -= 5
        self.assertEqual(cache.current()["vocabulary_version"], "2026-10-01")
        self.assertIn("interstate-war", cache.terms)


if __name__ == "__main__":
    unittest.main()
