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
                                   word_pattern, question_stems)
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
        self.put_member(GREEN, "notes/method.md",
                        b"# Method\nFields populate automatically where possible; the rest are typed. "
                        b"Dates were checked against the archive.\n")
        self.put_member(GREEN, "tiny.txt", b"test data\n")
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
        self.assertEqual(ix.stats(), {"passages": 4, "datasets": 2, "files": 4})
        self.assertEqual(ix.dims, DIM)
        self.assertTrue(os.path.exists(self.path))
        # Nothing changed: nothing fetched.
        self.assertEqual(ix.sync()["unchanged"], 2)
        # A file changes: re-read; an amber dataset appears: loaded.
        self.put_member(CLEAN, "method.txt", b"Different words about deaths in war.\n")
        self.write_passages((CLEAN, AMBER), ("green", "amber"))
        s = ix.sync()
        self.assertEqual((s["loaded"], s["unchanged"]), (2, 1))
        self.assertEqual(ix.stats()["passages"], 5)
        found = ix.search(bag("Different words about deaths in war"), limit=1)
        self.assertEqual((found[0]["identifier"], found[0]["member"]), (CLEAN, "method.txt"))
        # The file is deleted from the bucket: dropped from the copy.
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, AMBER))
        s = ix.sync()
        self.assertEqual(s["dropped"], 1)
        self.assertEqual(ix.stats()["passages"], 4)
        # Reopened from disk: still there, no reload needed.
        ix2 = self.index()
        self.assertEqual(ix2.sync()["unchanged"], 2)
        self.assertEqual(ix2.stats()["passages"], 4)

    def test_marker_rows_and_wrong_dims_are_left_out(self):
        self.put_member(GREEN, "scan.pdf", b"%PDF-1.4 nothing")       # unreadable: no text
        self.write_passages((GREEN,))
        ix = self.index()
        self.assertEqual(ix.sync()["loaded"], 1)
        self.assertEqual(ix.stats(), {"passages": 3, "datasets": 1, "files": 3})
        # A vector of the wrong length is refused quietly.
        self.assertEqual(ix.search([1.0] * (DIM + 1)), [])
        self.assertEqual(ix.search(bag("battles"), sensitivities=["amber"]), [])
        found = ix.search(bag("battles"), sensitivities=["green"])
        # The two-word passage is left out unless the words asked are in it.
        self.assertEqual(sorted(h["member"] for h in found), ["notes/method.md", "notes/readme.md"])
        self.assertEqual([h["why"] for h in found], ["meaning", "meaning"])
        found = ix.search(bag("x"), phrase="TEST data", stems=["test"])
        self.assertEqual([(h["member"], h["why"]) for h in found][0], ("tiny.txt", "phrase"))
        # Phrase first, then every word, then meaning.
        found = ix.search(bag("battles"), phrase="populate automatically where possible",
                          stems=question_stems("populate automatically where possible"))
        self.assertEqual([(h["member"], h["why"]) for h in found][:1], [("notes/method.md", "phrase")])
        found = ix.search(bag("battles"), phrase="possible to populate",
                          stems=question_stems("possible to populate"))
        self.assertEqual([(h["member"], h["why"]) for h in found][:1], [("notes/method.md", "words")])
        self.assertEqual(ix.search(bag("battles"), phrase="100% _sure_", stems=["100%"]),
                         ix.search(bag("battles")))       # odd characters do not break it

    def test_by_meaning_orders_the_whole_copy_by_similarity(self):
        """With a small limit, word matches must not crowd out a closer
        passage by meaning when the order asked for is meaning alone."""
        self.write_passages()
        ix = self.index()
        vec = bag("conflict deaths counted per year")
        every = sorted(ix.search(vec, limit=50), key=lambda h: -h["score"])
        best = every[0]
        # A word found in another passage but not in the closest one.
        word = next(w for h in every[1:] for w in h["text"].split()
                    if w.isalpha() and len(w) > 3 and w.lower() not in best["text"].lower())
        self.assertNotEqual(ix.search(vec, limit=1, phrase=word)[0]["text"], best["text"])
        top = ix.search(vec, limit=1, phrase=word, by_meaning=True)
        self.assertEqual(top[0]["text"], best["text"])
        self.assertEqual(top[0]["why"], "meaning")

    def test_a_phrase_is_matched_literally(self):
        self.put_member(GREEN, "codes.txt",
                        b"The file case_ref_12 holds the codes for each interview, 50% coded.\n")
        self.write_passages((GREEN,))
        ix = self.index()
        found = ix.search(bag("x"), phrase="case_ref_12")
        self.assertEqual([(h["member"], h["why"]) for h in found][:1], [("codes.txt", "phrase")])
        found = ix.search(bag("x"), phrase="50% coded")
        self.assertEqual([(h["member"], h["why"]) for h in found][:1], [("codes.txt", "phrase")])
        # _ and % are characters, not wildcards.
        found = ix.search(bag("x"), phrase="case ref 12")
        self.assertNotIn("phrase", [h["why"] for h in found])
        found = ix.search(bag("x"), phrase="case%12")
        self.assertNotIn("phrase", [h["why"] for h in found])
        self.assertEqual(ix.search(bag("x"), phrase="C:\\data\\"), ix.search(bag("x")))

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
        self.assertEqual(ix.stats()["passages"], 4)

    def test_open_index_falls_back_to_memory(self):
        bad = os.path.join(self.tmp.name, "afile")
        open(bad, "w").close()
        ix = open_index(s3mod.ReadOnly(self.s3), "crsw", PREFIX, bad + "/x/p.duckdb", 60, None)
        self.assertEqual(ix.path, ":memory:")
        ix = open_index(s3mod.ReadOnly(self.s3), "crsw", PREFIX, self.path, 60, DIM)
        self.assertEqual((ix.path, ix.dims), (self.path, DIM))

    def test_a_new_vector_size_rebuilds_the_copy(self):
        """The configured size wins over the one the copy was built with:
        the copy is emptied and refilled from the bucket, and files still
        at the old size are reported, not silently dropped."""
        self.write_passages()
        ix = self.index(dims=DIM)
        ix.sync()
        self.assertEqual(ix.stats()["passages"], 4)
        small = DIM // 2
        # The web is moved to the new size before the promoter rewrites.
        ix = self.index(dims=small)
        self.assertEqual(ix.dims, small)
        self.assertEqual(ix.stats()["passages"], 0)
        self.assertIn("2 passages files", ix.error)
        self.assertEqual(ix.search([1.0] * DIM), [])

        class Cut(FakePlatform):
            def embed(self, texts, batch=32):
                return [v[:small] for v in super().embed(texts, batch)]
        for ident in (CLEAN, GREEN):
            import json
            key = ident + "/dataset.%s.json" % ident.rsplit("/", 1)[-1]
            rec = json.loads(self.s3.get_object(Bucket="crsw", Key=key)["Body"].read())
            pmod.refresh(self.s3, "crsw", PREFIX, ident, rec, Cut(), "toy", small,
                         50 * 1024 * 1024, 1000)
        self.assertEqual(ix.sync()["loaded"], 2)
        self.assertIsNone(ix.error)
        self.assertEqual(ix.stats()["passages"], 4)
        self.assertTrue(ix.search(bag("battles")[:small]))
        # Reopened with the same size: nothing rebuilt.
        self.assertEqual(self.index(dims=small).sync()["unchanged"], 2)

    def test_a_file_that_will_not_load_is_not_fetched_again_until_it_changes(self):
        self.write_passages()
        fetched = []
        client = s3mod.ReadOnly(self.s3)

        class Counting:
            def get_paginator(self, name):
                return client.get_paginator(name)

            def get_object(self, **kw):
                fetched.append(kw["Key"])
                return client.get_object(**kw)
        ix = PassageIndex(Counting(), "crsw", PREFIX, self.path, dims=DIM // 2)
        self.assertEqual(ix.sync()["skipped"], 2)
        self.assertEqual(len(fetched), 2)
        s = ix.sync()
        self.assertEqual((s["skipped"], len(fetched)), (2, 2))    # remembered, not fetched
        self.assertIn("2 passages files not loaded", ix.error)
        # The file changes in the bucket: fetched again.
        self.put_member(CLEAN, "method.txt", b"New words for the method file.\n")
        self.write_passages((CLEAN,))
        ix.sync()
        self.assertEqual(len(fetched), 3)

    def test_a_fetch_that_fails_once_is_tried_again(self):
        """A network or storage error is not the file's fault: the next
        sync fetches it again (unlike a file that will not parse)."""
        self.write_passages()
        client = s3mod.ReadOnly(self.s3)
        failures = {pmod.passages_key(PREFIX, CLEAN): 1}

        class Flaky:
            def get_paginator(self, name):
                return client.get_paginator(name)

            def get_object(self, **kw):
                if failures.get(kw["Key"]):
                    failures[kw["Key"]] -= 1
                    raise RuntimeError("connection reset")
                return client.get_object(**kw)
        ix = PassageIndex(Flaky(), "crsw", PREFIX, self.path, dims=DIM)
        s = ix.sync()
        self.assertEqual((s["loaded"], s["skipped"]), (1, 1))
        self.assertIn("connection reset", ix.error)
        s = ix.sync()
        self.assertEqual((s["loaded"], s["skipped"]), (1, 0))
        self.assertIsNone(ix.error)
        self.assertEqual(ix.stats()["passages"], 4)

    def test_search_during_a_sync_sees_whole_files(self):
        """Readers in other threads never see a file half replaced, and
        never share the syncing thread's connection."""
        import threading
        key = pmod.passages_key(PREFIX, CLEAN)
        self.write_passages((CLEAN,))
        first = self.s3.get_object(Bucket="crsw", Key=key)["Body"].read()
        self.put_member(CLEAN, "method.txt", b"Conflict deaths are counted per year, again.\n")
        self.write_passages((CLEAN,))
        second = self.s3.get_object(Bucket="crsw", Key=key)["Body"].read()
        ix = self.index(refresh_seconds=3600)
        ix.sync()
        stop, problems = threading.Event(), []

        def churn():
            try:
                for i in range(40):
                    self.s3.put_object(Bucket="crsw", Key=key, Body=(first, second)[i % 2])
                    ix.sync()
            except Exception as e:
                problems.append("sync: %r" % e)
            finally:
                stop.set()

        def read():
            while not stop.is_set():
                try:
                    found = ix.search(bag("conflict deaths counted"), limit=50)
                    if not any(h["identifier"] == CLEAN for h in found):
                        problems.append("search saw no passages of %s" % CLEAN)
                    ix.stats()
                except Exception as e:
                    problems.append("search: %r" % e)

        threads = ([threading.Thread(target=churn, daemon=True)] +
                   [threading.Thread(target=read, daemon=True) for _ in range(3)])
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual([t for t in threads if t.is_alive()], [], "threads stuck")
        self.assertEqual(problems[:3], [])


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
        self.assertEqual(question_stems("Populate automatically, where possible!"),
                         ["popul", "autom", "possi"])
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
        self.assertEqual([h["member"] for h in body["passages"]][:2], ["method.txt", "notes/method.md"])
        self.assertEqual(body["passages"][0]["why"], "words")          # conflict, deaths, year
        self.assertEqual(body["passages"][1]["why"], "meaning")
        # Why it matched: the closest sentence of the two-sentence passage.
        sentences = body["passages"][0]["sentences"]
        self.assertEqual([s["text"] for s in sentences],
                         ["Conflict deaths are counted per year from the CSAC database.",
                          "Each event is coded by location and actor."])
        self.assertEqual([(s["marked"], s["how"]) for s in sentences], [(True, "words"), (False, None)])
        # Only the passages found by meaning went back to the platform for
        # their closest sentence, all together; the one found by its words
        # did not need to.
        self.assertEqual(len(p.embed_calls), 2)               # question, then sentences
        self.assertNotIn(sentences[0]["text"], p.embed_calls[-1])
        self.assertIn("Dates were checked against the archive.", p.embed_calls[-1])
        self.assertEqual(body["passages"][0]["dataset"], "csac-clean")
        self.assertIn("Conflict deaths", body["passages"][0]["text"])
        self.assertEqual(body["stats"]["passages"], 5)        # held, but amber never queried
        # Amber served to all: its passage appears; its text goes to the
        # reranker only when amber may be sent (a query that finds it by
        # meaning, not by its words, so it is among what is reranked).
        c = self.app(platform=FakePlatform(), amber_access="all")
        body = self.hits(c, "survivor of forced marriage")
        self.assertIn(AMBER, [h["identifier"] for h in body["passages"]])
        p = FakePlatform()
        self.app(platform=p, amber_access="all").get("/search.json", params={"q": "interview transcripts"})
        self.assertFalse(any("survivor" in d.lower() for _, docs in p.rerank_calls for d in docs))
        p = FakePlatform()
        self.app(platform=p, amber_access="all",
                 llm_sensitivities=("green", "amber")).get("/search.json", params={"q": "interview transcripts"})
        self.assertTrue(any("survivor" in d.lower() for _, docs in p.rerank_calls for d in docs))
        # Groups: only the strand's members see the amber passage.
        c = self.app(platform=FakePlatform(), amber_access="groups",
                     dev_groups=("er_prj_kdl_slavery_rs2",))
        self.assertNotIn(AMBER, [h["identifier"] for h in self.hits(c, "survivor")["passages"]])
        c = self.app(platform=FakePlatform(), amber_access="groups",
                     dev_groups=("er_prj_kdl_slavery_rs1",))
        self.assertIn(AMBER, [h["identifier"] for h in self.hits(c, "survivor")["passages"]])

    def test_typing_back_a_phrase_finds_it_and_marks_it(self):
        self.write_passages((CLEAN, GREEN))
        p = FakePlatform(reverse=True)                  # a reranker that would bury it
        body = self.hits(self.app(platform=p), "populate automatically where possible")
        first = body["passages"][0]
        self.assertEqual((first["member"], first["why"]), ("notes/method.md", "phrase"))
        self.assertEqual([(s["marked"], s["how"]) for s in first["sentences"]],
                         [(True, "phrase"), (False, None)])
        # The two-word passage is not among the results at all.
        self.assertNotIn("tiny.txt", [h["member"] for h in body["passages"]])
        # ...unless those are the words asked for.
        body = self.hits(self.app(platform=FakePlatform()), "test data")
        self.assertEqual((body["passages"][0]["member"], body["passages"][0]["why"]), ("tiny.txt", "phrase"))
        html = self.app(platform=FakePlatform()).get(
            "/search", params={"q": "populate automatically where possible"}).text
        self.assertIn('<mark class="phrase" title="contains your exact words"># Method Fields <b>populate</b> <b>automatically</b> where <b>possible</b>; the rest are typed.</mark>', html)
        self.assertIn('<span class="why">contains your exact words</span>', html)
        self.assertIn('<span class="why">closest in meaning</span>', html)

    def test_page_and_degradation(self):
        self.write_passages()
        c = self.app(platform=FakePlatform())
        html = c.get("/search", params={"q": "battles and massacres"}).text
        self.assertIn("4 passages from 4 files in 2 datasets", html)
        self.assertIn('<a href="/datasets/%s/files/notes/readme.md">notes/readme.md</a>' % GREEN, html)
        self.assertIn("<b>battles</b> and <b>massacres</b>", html)
        self.assertIn("<h1>Search inside content</h1>", html)
        # The two-sentence passage: its closest sentence marked, the question's
        # stem bold inside it (deaths -> "death" stem matches "deaths").
        html = c.get("/search", params={"q": "conflict deaths"}).text
        self.assertIn('<mark class="phrase" title="contains your exact words"><b>Conflict</b> <b>deaths</b> are counted per year from the CSAC database.</mark>', html)
        self.assertIn(" Each event is coded by location and actor. ", html)
        self.assertNotIn(">Each event", html.split("</mark>")[0])
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
        self.assertFalse(any(s["how"] == "meaning" for h in body["passages"] for s in h["sentences"]))
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
        # The reranker's order wins among passages found by meaning.
        straight = [h["member"] for h in self.hits(self.app(platform=FakePlatform()), "archive")["passages"]
                    if h["why"] == "meaning"]
        reversed_ = [h["member"] for h in self.hits(self.app(platform=FakePlatform(reverse=True)), "archive")["passages"]
                     if h["why"] == "meaning"]
        self.assertGreater(len(straight), 1)
        self.assertEqual(reversed_, straight[::-1])

    def test_weak_meaning_only_results_are_folded(self):
        self.write_passages((CLEAN, GREEN))
        c = self.app(platform=FakePlatform())
        # Word matches present: every meaning-only result is weak and folded.
        body = self.hits(c, "populate automatically where possible")
        self.assertEqual([h["weak"] for h in body["passages"] if h["why"] != "meaning"], [False])
        self.assertTrue(all(h["weak"] for h in body["passages"] if h["why"] == "meaning"))
        html = c.get("/search", params={"q": "populate automatically where possible"}).text
        self.assertIn("<summary>2 more passages, close in meaning only</summary>", html)
        self.assertLess(html.index("contains your exact words"), html.index("<details"))
        self.assertNotIn("Nothing contains your words", html)
        # No word matches: those within the band of the best stay open, the
        # rest fold; the page says what it is showing.
        body = self.hits(c, "zzzz")
        scores = [h["score"] for h in body["passages"]]
        best = max(scores)
        self.assertEqual([h["weak"] for h in body["passages"]],
                         [s < best - 0.05 for s in scores])
        html = c.get("/search", params={"q": "zzzz"}).text
        if all(h["weak"] for h in body["passages"]):
            self.assertIn("Nothing contains your words.", html)
        else:
            self.assertNotIn("Nothing contains your words.", html)
        # Same rule under sort=meaning.
        body = self.hits(c, "populate automatically where possible", sort="meaning")
        self.assertTrue(all(h["weak"] for h in body["passages"] if h["why"] == "meaning"))

    def test_sort_by_meaning_alone(self):
        self.write_passages((CLEAN, GREEN))
        q = "populate automatically where possible"
        c = self.app(platform=FakePlatform())
        words_first = self.hits(c, q)
        self.assertEqual(words_first["sort"], "words")
        self.assertEqual(words_first["passages"][0]["why"], "phrase")
        by_meaning = self.hits(c, q, sort="meaning")
        self.assertEqual(by_meaning["sort"], "meaning")
        scores = [h["score"] for h in by_meaning["passages"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # Every result still says why, and the phrase hit is still marked green.
        phrase = next(h for h in by_meaning["passages"] if h["why"] == "phrase")
        self.assertTrue(any(s["how"] == "phrase" for s in phrase["sentences"]))
        self.assertEqual(self.hits(c, q, sort="nonsense")["sort"], "words")
        html = c.get("/search", params={"q": q, "sort": "meaning"}).text
        self.assertIn('value="meaning" checked', html)
        self.assertNotIn('value="words" checked', html)

    def test_by_meaning_alone_is_not_reranked(self):
        """The reranker would reorder (and push amber back); by meaning
        alone the list stays in order of similarity."""
        self.write_passages((CLEAN, GREEN, AMBER), ("green", "amber"))
        p = FakePlatform(reverse=True)
        c = self.app(platform=p, amber_access="all")
        body = self.hits(c, "conflict deaths events battles", sort="meaning")
        scores = [h["score"] for h in body["passages"]]
        self.assertGreater(len(scores), 2)
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertNotIn("rerank", body["steps"])
        self.assertEqual(p.rerank_calls, [])
        self.assertIn("rerank", self.hits(c, "conflict deaths events battles")["steps"])

    def test_why_this_on_demand(self):
        self.write_passages((CLEAN, GREEN, AMBER), ("green", "amber"))
        p = FakePlatform()
        c = self.app(platform=p, amber_access="all")
        html = c.get("/search", params={"q": "archive"}).text
        self.assertIn('class="link why-ask" data-identifier="%s" data-member="notes/method.md" data-position="0"' % GREEN, html)
        self.assertIn('<script src="/static/search.js" defer></script>', html)
        # No button for the amber passage: its text may not be sent.
        self.assertNotIn('data-identifier="%s"' % AMBER, html)
        self.assertEqual(getattr(p, "explain_calls", []), [])      # nothing asked yet
        r = c.get("/search/why", params={"q": "archive", "identifier": GREEN,
                                         "member": "notes/method.md", "position": 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"why": "It mentions #.", "notice": None})
        self.assertEqual(p.explain_calls[0][0], "archive")
        self.assertIn("Dates were checked", p.explain_calls[0][1])
        # Amber: refused even when downloadable, until amber may be sent.
        r = c.get("/search/why", params={"q": "archive", "identifier": AMBER, "member": "t1.txt", "position": 0})
        self.assertEqual(r.status_code, 403)
        self.assertIn("sensitivity", r.json()["detail"])
        c2 = self.app(platform=FakePlatform(), amber_access="all", llm_sensitivities=("green", "amber"))
        self.assertEqual(c2.get("/search/why", params={"q": "archive", "identifier": AMBER,
                                                        "member": "t1.txt", "position": 0}).status_code, 200)
        self.assertIn('data-identifier="%s"' % AMBER, c2.get("/search", params={"q": "archive"}).text)
        # Not downloadable: refused; unknown passage: 404; blank question: 404.
        c3 = self.app(platform=FakePlatform())                      # amber off
        self.assertEqual(c3.get("/search/why", params={"q": "archive", "identifier": AMBER,
                                                        "member": "t1.txt", "position": 0}).status_code, 403)
        self.assertEqual(c.get("/search/why", params={"q": "archive", "identifier": GREEN,
                                                       "member": "nope.txt", "position": 0}).status_code, 404)
        self.assertEqual(c.get("/search/why", params={"q": " ", "identifier": GREEN,
                                                       "member": "notes/method.md", "position": 0}).status_code, 404)
        # Platform down: 503 with a notice, no crash.
        r = self.app(platform=FakePlatform(fail=("explain",))).get(
            "/search/why", params={"q": "archive", "identifier": GREEN, "member": "notes/method.md", "position": 0})
        self.assertEqual(r.status_code, 503)
        self.assertIn("No explanation", r.json()["notice"])
        # Chat model off: no button, route absent.
        c4 = self.app(platform=FakePlatform(off=("chat",)))
        self.assertNotIn("why-ask", c4.get("/search", params={"q": "archive"}).text)
        self.assertEqual(c4.get("/search/why", params={"q": "archive", "identifier": GREEN,
                                                        "member": "notes/method.md", "position": 0}).status_code, 404)

    def test_search_script_parses(self):
        import shutil, subprocess, tempfile, os as _os
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        js = self.app(platform=FakePlatform()).get("/static/search.js").text
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(js)
            run = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        finally:
            _os.unlink(path)
        # Nothing in the copy yet: an empty page, no error.
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, CLEAN))
        self.s3.delete_object(Bucket="crsw", Key=pmod.passages_key(PREFIX, GREEN))
        c = self.app(platform=FakePlatform(), passages_path=os.path.join(self.tmp.name, "n.duckdb"))
        body = self.hits(c, "battles")
        self.assertEqual((body["passages"], body["stats"]["passages"]), ([], 0))
