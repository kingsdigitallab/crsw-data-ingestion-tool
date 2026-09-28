"""Searching inside documents, the web side: the VM's copy of the
passages kept in step with the bucket, search by meaning, the download
rule applied to every hit, the reranker over the top, and the whole
thing off unless switched on."""
import os
import tempfile
import unittest

from tests.test_web_ask import (AskBase, FakePlatform, bag, ASK, CLEAN, GREEN, AMBER, DIM)
from tests.test_web_catalogue import HAVE_WEB, VOCAB, READ

if HAVE_WEB:
    from fastapi.testclient import TestClient
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    from crsw_web.passages import (PassageIndex, open_index, split_sentences, mark_closest,
                                   word_pattern)
    from promoter import passages as pmod
    from promoter import index as index_mod

SEARCH = dict(ASK, passages=True, llm_embed_dims=DIM)
PREFIX = "index"


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class SearchBase(AskBase):
    def seed(self):
        super().seed()
        # Documents in three datasets: two green, one amber.
        self.put_member(CLEAN, "method.txt",
                        b"Conflict deaths are counted per year from the CSAC database. "
                        b"Each event is coded by location and actor.\n")
        self.put_member(GREEN, "notes/readme.md",
                        b"# Events\nThe events file lists battles and massacres with dates.\n")
        self.put_member(AMBER, "t1.txt", b"Interview with a survivor of forced marriage.\n")
        self.rows_by_id = {r["identifier"]: r for r in index_mod.rows(self.s3, "crsw")}
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "copy", "passages.duckdb")

    def put_member(self, identifier, member, data):
        """Add or replace one file of a dataset, record and object both."""
        import hashlib, json
        key = identifier + "/dataset.%s.json" % identifier.rsplit("/", 1)[-1]
        rec = json.loads(self.s3.get_object(Bucket="crsw", Key=key)["Body"].read())
        files = [f for f in rec["files"] if f["path"] != member]
        files.append({"path": member, "checksum_sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data), "format": member.rsplit(".", 1)[-1]})
        rec["files"] = sorted(files, key=lambda f: f["path"])
        self.s3.put_object(Bucket="crsw", Key=key, Body=json.dumps(rec).encode())
        self.s3.put_object(Bucket="crsw", Key=identifier + "/" + member, Body=data)

    def write_passages(self, identifiers=(CLEAN, GREEN), sensitivities=("green",)):
        import json
        for ident in identifiers:
            key = ident + "/dataset.%s.json" % ident.rsplit("/", 1)[-1]
            rec = json.loads(self.s3.get_object(Bucket="crsw", Key=key)["Body"].read())
            if rec["sensitivity"] not in sensitivities:
                continue
            pmod.refresh(self.s3, "crsw", PREFIX, ident, rec, FakePlatform(), "toy", None,
                         50 * 1024 * 1024, 1000)

    def index(self, path=None, **kw):
        return PassageIndex(s3mod.ReadOnly(self.s3), "crsw", PREFIX,
                            path or self.path, **kw)

    def app(self, platform=None, index=None, **overrides):
        values = dict(SEARCH, passages_path=self.path)
        values.update(overrides)
        settings = Settings(**values)
        return TestClient(create_app(settings, s3_client=self.s3, vocab_dict=VOCAB,
                                     read_client=s3mod.ReadOnly(self.s3), platform=platform,
                                     passage_index=index))

    def hits(self, client, q, **params):
        r = client.get("/search.json", params=dict(q=q, **params))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestOff(SearchBase):
    def test_off_unless_switched_on_and_platform_present(self):
        self.write_passages()
        c = TestClient(create_app(Settings(**ASK), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3), platform=FakePlatform()))
        self.assertEqual(c.get("/search", params={"q": "x"}).status_code, 404)
        self.assertEqual(c.get("/search.json", params={"q": "x"}).status_code, 404)
        self.assertNotIn("Search inside content", c.get("/datasets").text)
        # Switched on but no platform: still off.
        c = TestClient(create_app(Settings(**dict(READ, passages=True)), s3_client=self.s3,
                                  vocab_dict=VOCAB, read_client=s3mod.ReadOnly(self.s3)))
        self.assertEqual(c.get("/search").status_code, 404)
        s = Settings(**dict(SEARCH, llm_embed_model=""))
        self.assertFalse(s.passages_enabled)
        self.assertTrue(Settings(**SEARCH).passages_enabled)

    def test_config(self):
        env = {"CRSW_S3_ENDPOINT": "https://rgw.example", "CRSW_S3_ACCESS_KEY": "t",
               "CRSW_S3_SECRET_KEY": "t", "CRSW_S3_BUCKET": "crsw",
               "CRSW_STAGING_PREFIX": "staging/_test"}
        s = Settings.from_env(env)
        self.assertEqual((s.passages, s.passages_path), (False, "/data/passages.duckdb"))
        s = Settings.from_env(dict(env, CRSW_PASSAGES="1", CRSW_PASSAGES_PATH="/tmp/p.duckdb"))
        self.assertEqual((s.passages, s.passages_path), (True, "/tmp/p.duckdb"))
        self.assertFalse(s.passages_enabled)      # no read role, no platform


class TestCopy(SearchBase):
    def test_sync_loads_changes_and_drops(self):
        self.write_passages()
        ix = self.index()
        s = ix.sync()
        self.assertEqual((s["loaded"], s["dropped"], s["unchanged"], s["skipped"]), (2, 0, 0, 0))
        self.assertEqual(ix.stats(), {"passages": 2, "datasets": 2, "files": 2})
        self.assertEqual(ix.dims, DIM)
        self.assertTrue(os.path.exists(self.path))
        # Nothing changed: nothing fetched.
        self.assertEqual(ix.sync()["unchanged"], 2)
        # A file changes: re-read; an amber dataset appears: loaded.
        self.put_member(CLEAN, "method.txt", b"Different words about deaths in war.\n")
        self.write_passages((CLEAN, AMBER), ("green", "amber"))
        s = ix.sync()
        self.assertEqual((s["loaded"], s["unchanged"]), (2, 1))
        self.assertEqual(ix.stats()["passages"], 3)
        found = ix.search(bag("Different words about deaths in war"), limit=1)
        self.assertEqual((found[0]["identifier"], found[0]["member"]), (CLEAN, "method.txt"))
        # The file is deleted from the bucket: dropped from the copy.
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, AMBER))
        s = ix.sync()
        self.assertEqual(s["dropped"], 1)
        self.assertEqual(ix.stats()["passages"], 2)
        # Reopened from disk: still there, no reload needed.
        ix2 = self.index()
        self.assertEqual(ix2.sync()["unchanged"], 2)
        self.assertEqual(ix2.stats()["passages"], 2)

    def test_marker_rows_and_wrong_dims_are_left_out(self):
        self.put_member(GREEN, "scan.pdf", b"%PDF-1.4 nothing")       # unreadable: no text
        self.write_passages((GREEN,))
        ix = self.index()
        self.assertEqual(ix.sync()["loaded"], 1)
        self.assertEqual(ix.stats(), {"passages": 1, "datasets": 1, "files": 1})
        # A vector of the wrong length is refused quietly.
        self.assertEqual(ix.search([1.0] * (DIM + 1)), [])
        self.assertEqual(ix.search(bag("battles"), sensitivities=["amber"]), [])
        self.assertEqual(len(ix.search(bag("battles"), sensitivities=["green"])), 1)

    def test_listing_failure_keeps_the_copy(self):
        self.write_passages()
        ix = self.index(refresh_seconds=0)
        ix.sync()
        class Broken:
            def get_paginator(self, name):
                raise RuntimeError("bucket gone")
            def get_object(self, **kw):
                raise RuntimeError("bucket gone")
        ix._client = Broken()
        self.assertEqual(ix.sync()["loaded"], 0)
        self.assertIn("listing failed", ix.error)
        self.assertEqual(ix.stats()["passages"], 2)

    def test_open_index_falls_back_to_memory(self):
        bad = os.path.join(self.tmp.name, "afile")
        open(bad, "w").close()
        ix = open_index(s3mod.ReadOnly(self.s3), "crsw", PREFIX, bad + "/x/p.duckdb", 60, None)
        self.assertEqual(ix.path, ":memory:")
        ix = open_index(s3mod.ReadOnly(self.s3), "crsw", PREFIX, self.path, 60, DIM)
        self.assertEqual((ix.path, ix.dims), (self.path, DIM))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestWhy(unittest.TestCase):
    def test_split_sentences(self):
        self.assertEqual(split_sentences("One. Two? Three! four. Five"),
                         ["One.", "Two?", "Three! four.", "Five"])
        self.assertEqual(split_sentences("Sidecar spec v0.5. Next item"), ["Sidecar spec v0.5.", "Next item"])
        self.assertEqual(split_sentences("line one\nline two"), ["line one", "line two"])
        self.assertEqual(split_sentences('He said "Go." "Now."'), ['He said "Go."', '"Now."'])
        self.assertEqual(split_sentences("test data"), ["test data"])
        self.assertEqual(split_sentences(""), [""])

    def test_mark_closest(self):
        q = [1.0, 0.0]
        self.assertEqual(mark_closest([[1.0, 0.0], [0.0, 1.0], [0.99, 0.1]], q), [True, False, True])
        self.assertEqual(mark_closest([[1.0, 0.0], [0.0, 1.0]], q, margin=0.0), [True, False])
        self.assertEqual(mark_closest([[1.0, 0.0], [0.99, 0.1], [0.98, 0.15], [1.0, 0.01]], q),
                         [True, False, False, True])                     # at most two
        self.assertEqual(mark_closest([], q), [])
        self.assertEqual(mark_closest([[1.0, 0.0, 0.0]], q), [False])      # wrong length

    def test_word_pattern(self):
        pat = word_pattern("how are uploads verified, e.g. by size?")
        found = [m.group(0) for m in pat.finditer("Upload verification compares sizes; verified by the promoter. Size matters.")]
        self.assertEqual(found, ["Upload", "verification", "sizes", "verified", "Size"])
        self.assertIsNone(word_pattern("a an of"))
        self.assertIsNone(word_pattern("what about this and that"))     # stop words only
        self.assertIsNone(word_pattern(""))
        self.assertEqual(word_pattern("CSAC").pattern.count("csac"), 1)


class TestSearchPage(SearchBase):
    def test_search_by_meaning_with_the_download_rule(self):
        self.write_passages((CLEAN, GREEN, AMBER), ("green", "amber"))
        p = FakePlatform()
        c = self.app(platform=p)
        html = c.get("/datasets").text
        self.assertIn('href="/search">Search inside content</a>', html)
        for path in ("/", "/datasets/" + GREEN):
            self.assertIn('href="/search">Search inside content</a>', c.get(path).text, path)
        body = self.hits(c, "conflict deaths per year")
        self.assertEqual(body["steps"], ["meaning", "rerank", "highlight"])
        self.assertEqual([h["identifier"] for h in body["passages"]], [CLEAN, GREEN])
        self.assertEqual(body["passages"][0]["member"], "method.txt")
        # Why it matched: the closest sentence of the two-sentence passage.
        sentences = body["passages"][0]["sentences"]
        self.assertEqual([s["text"] for s in sentences],
                         ["Conflict deaths are counted per year from the CSAC database.",
                          "Each event is coded by location and actor."])
        self.assertEqual([s["marked"] for s in sentences], [True, False])
        # The sentences went to the platform once, all together; the
        # one-sentence passage was not sent.
        self.assertEqual(p.embed_calls[-1], [s["text"] for s in sentences])
        self.assertEqual(len(p.embed_calls), 2)               # question, then sentences
        self.assertEqual(body["passages"][0]["dataset"], "csac-clean")
        self.assertIn("Conflict deaths", body["passages"][0]["text"])
        self.assertEqual(body["stats"]["passages"], 3)        # held, but amber never queried
        # Amber served to all: its passage appears and can be reranked only
        # when amber may be sent.
        c = self.app(platform=FakePlatform(), amber_access="all")
        body = self.hits(c, "survivor of forced marriage")
        self.assertIn(AMBER, [h["identifier"] for h in body["passages"]])
        p = FakePlatform()
        self.app(platform=p, amber_access="all").get("/search.json", params={"q": "survivor"})
        self.assertFalse(any("survivor" in d.lower() for _, docs in p.rerank_calls for d in docs))
        p = FakePlatform()
        self.app(platform=p, amber_access="all",
                 llm_sensitivities=("green", "amber")).get("/search.json", params={"q": "survivor"})
        self.assertTrue(any("survivor" in d.lower() for _, docs in p.rerank_calls for d in docs))
        # Groups: only the strand's members see the amber passage.
        c = self.app(platform=FakePlatform(), amber_access="groups",
                     dev_groups=("er_prj_kdl_slavery_rs2",))
        self.assertNotIn(AMBER, [h["identifier"] for h in self.hits(c, "survivor")["passages"]])
        c = self.app(platform=FakePlatform(), amber_access="groups",
                     dev_groups=("er_prj_kdl_slavery_rs1",))
        self.assertIn(AMBER, [h["identifier"] for h in self.hits(c, "survivor")["passages"]])

    def test_page_and_degradation(self):
        self.write_passages()
        c = self.app(platform=FakePlatform())
        html = c.get("/search", params={"q": "battles and massacres"}).text
        self.assertIn("2 passages from 2 files in 2 datasets", html)
        self.assertIn('<a href="/datasets/%s/files/notes/readme.md">notes/readme.md</a>' % GREEN, html)
        self.assertIn("<b>battles</b> and <b>massacres</b>", html)
        self.assertIn("<h1>Search inside content</h1>", html)
        # The two-sentence passage: its closest sentence marked, the question's
        # stem bold inside it (deaths -> "death" stem matches "deaths").
        html = c.get("/search", params={"q": "conflict deaths"}).text
        self.assertIn("<mark><b>Conflict</b> <b>deaths</b> are counted per year from the CSAC database.</mark>", html)
        self.assertIn(" Each event is coded by location and actor. ", html)
        self.assertNotIn("<mark>Each event", html)
        # HTML in a passage is escaped, not rendered.
        self.put_member(GREEN, "notes/readme.md", b"# Events\nSee <script>alert(1)</script> for battles.\n")
        self.write_passages((GREEN,))
        html = self.app(platform=FakePlatform(), passages_path=os.path.join(self.tmp.name, "e.duckdb")).get(
            "/search", params={"q": "battles"}).text
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)
        html = c.get("/search").text
        self.assertIn('id="q"', html)
        self.assertNotIn("Nothing close enough", html)
        # Only the highlighting call down: passages unmarked, a notice.
        class FlakyPlatform(FakePlatform):
            def embed(self, texts, batch=32):
                if self.embed_calls:
                    self.embed_calls.append(list(texts))
                    raise PlatformError("platform returned HTTP 503 for /embeddings")
                return super().embed(texts, batch)
        from crsw_web.llm import PlatformError
        body = self.hits(self.app(platform=FlakyPlatform()), "conflict deaths per year")
        self.assertEqual(body["steps"], ["meaning", "rerank"])
        self.assertIn("Highlighting is unavailable", body["notice"])
        self.assertTrue(body["passages"])
        self.assertFalse(any(s["marked"] for h in body["passages"] for s in h["sentences"]))
        # Embedding down: a notice, not an error.
        p = FakePlatform(fail=("embed",))
        body = self.hits(self.app(platform=p), "battles")
        self.assertEqual(body["passages"], [])
        self.assertIn("unavailable", body["notice"])
        r = self.app(platform=p).get("/search", params={"q": "battles"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("unavailable", r.text)
        # Reranker down or off: the vector order stands.
        body = self.hits(self.app(platform=FakePlatform(fail=("rerank",))), "battles")
        self.assertEqual(body["steps"], ["meaning", "highlight"])
        self.assertIn("reranker", body["notice"])
        body = self.hits(self.app(platform=FakePlatform(off=("rerank",))), "battles")
        self.assertEqual((body["steps"], body["notice"]), (["meaning", "highlight"], None))
        # The reranker's order wins.
        body = self.hits(self.app(platform=FakePlatform(reverse=True)), "conflict deaths per year")
        self.assertEqual([h["identifier"] for h in body["passages"]], [GREEN, CLEAN])
        # Nothing in the copy yet: an empty page, no error.
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, CLEAN))
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, GREEN))
        c = self.app(platform=FakePlatform(), passages_path=os.path.join(self.tmp.name, "n.duckdb"))
        body = self.hits(c, "battles")
        self.assertEqual((body["passages"], body["stats"]["passages"]), ([], 0))
