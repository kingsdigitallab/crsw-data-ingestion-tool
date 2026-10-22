"""Search by meaning or content, the web side: the platform fixtures,
the VM's copy of the passages kept in step with the bucket, dataset
descriptions and passages ranked by meaning, the download rule applied
to every passage, the reranker over the top, and the whole thing off
unless the embedding model is on."""
import os
import tempfile
import unittest
from unittest import mock

from tests.test_web_catalogue import (HAVE_WEB, CatalogueBase, META, GREEN, AMBER, READ,
                                      SETTINGS, VOCAB)

if HAVE_WEB:
    import duckdb
    from fastapi.testclient import TestClient
    from crsw_deposit import record
    from crsw_web import llm as llm_mod
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.catalogue import Catalogue
    from crsw_web.config import ConfigError, Settings
    from crsw_web.llm import PlatformError
    from crsw_web.passages import (PassageIndex, open_index, split_sentences, mark_closest,
                                   word_pattern, question_stems)
    from promoter import embed as embed_mod
    from promoter import index as index_mod
    from promoter import passages as pmod

CLEAN = "rs2/csac/green/2_final/csac-clean"
LLM = dict(READ, llm_base_url="https://ai.example/api/v1", llm_api_key="sk-test")
DIM = 8
SEARCH = dict(LLM, passages=True, llm_embed_dims=DIM)
PREFIX = "index"


def bag(text):
    """A deterministic toy embedding: letters folded into eight bins, so
    texts that share words land near each other."""
    v = [0.0] * DIM
    for word in text.lower().split():
        v[sum(ord(c) for c in word) % DIM] += 1.0
    return v


class FakePlatform:
    """Records what it was sent; each method can be told to fail."""

    chat_model, embed_model, rerank_model = "arc:lite", "arc:embedvl", "arc:rerankvl"
    fingerprint = "toy"      # what the stores the fixtures write are keyed to

    def __init__(self, fail=(), reverse=False, off=(), fingerprint=None):
        self.fail = set(fail)
        self.reverse = reverse              # a reranker that disagrees, so it shows
        for name in off:                    # a blank model name is the off switch
            setattr(self, name + "_model", "")
        if fingerprint is not None:         # the alias repointed to another backend
            self.fingerprint = fingerprint
        self.embed_calls, self.rerank_calls, self.explain_calls = [], [], []
        self.embed_instructions = []        # one per embed call: the instruction, or None

    def embed_fingerprint(self):
        return self.fingerprint

    def embed(self, texts, batch=32, instruction=None):
        self.embed_calls.append(list(texts))
        self.embed_instructions.append(instruction)
        if "embed" in self.fail:
            raise PlatformError("platform unreachable: ConnectError")
        return [bag(t) for t in texts]

    def explain(self, question, passage):
        self.explain_calls.append((question, passage))
        if "explain" in self.fail:
            raise PlatformError("platform returned HTTP 503 for /chat/completions")
        return "It mentions %s." % passage.split()[0]

    def rerank(self, question, documents):
        self.rerank_calls.append((question, list(documents)))
        if "rerank" in self.fail:
            raise PlatformError("platform returned HTTP 404 for /rerank")
        order = list(range(len(documents)))
        return order[::-1] if self.reverse else order


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class PlatformBase(CatalogueBase):
    """The catalogue's three records plus a cleaned one, the index
    written, and a platform fake; the embeddings file on request."""

    def seed(self):
        super().seed()
        clean = dict(META, state="2_final", dataset="csac-clean", subject=["armed-conflict"],
                     coverage_start="1989", coverage_end="2025", creator="Cy",
                     abstract="Conflict deaths per year, cleaned from the CSAC database. " * 4)
        self.put_record(CLEAN, clean, [record.manifest_entry("clean.csv", "d" * 64, 300)],
                        "k3", "33333333-3333-4333-8333-333333333333")
        rows = index_mod.rows(self.s3, "crsw")
        index_mod.write(self.s3, "crsw", "index", rows)
        self.index_rows = rows

    def embed_store(self, include_amber=False, model="toy"):
        embed_mod.refresh(self.s3, "crsw", "index", self.index_rows, FakePlatform(),
                          model, {"green", "amber"} if include_amber else {"green"})

    def rekey(self, identifier, model):
        """Rewrite the store with one dataset's vector keyed to `model`,
        as it is while the promoter is between two embedding models."""
        rows = embed_mod.read_existing(self.s3, "crsw", "index/embeddings.parquet")
        rows[identifier]["model"] = model
        embed_mod.write(self.s3, "crsw", "index", list(rows.values()))

    def app(self, platform=None, vocab=None, **overrides):
        settings = Settings(**dict(LLM, **overrides))
        return TestClient(create_app(settings, s3_client=self.s3, vocab_dict=vocab or VOCAB,
                                     read_client=s3mod.ReadOnly(self.s3), platform=platform))

    def ids(self, client, **params):
        return [d["identifier"] for d in client.get("/datasets.json", params=params).json()["datasets"]]


class TestFindData(PlatformBase):
    def test_no_question_box_and_a_pointer_to_the_search_page(self):
        """The question-to-filter step was retired on 22 October 2026:
        `ask` is ignored, nothing goes to the platform from Find data,
        and the page says where meaning search lives."""
        self.embed_store()
        p = FakePlatform()
        c = self.app(platform=p)
        html = c.get("/datasets", params={"ask": "conflict deaths"}).text
        self.assertNotIn('id="ask"', html)
        self.assertNotIn("Understood as", html)
        self.assertEqual(html.count('<tr class="dataset">'), 3)       # ask ignored
        self.assertEqual((p.embed_calls, p.rerank_calls), ([], []))
        self.assertIn('use <a href="/search">Search by meaning or content</a>', html)
        body = c.get("/datasets.json", params={"ask": "conflict deaths"}).json()
        self.assertNotIn("understood", body)
        self.assertEqual(len(body["datasets"]), 3)
        # Without the platform there is nothing to point to.
        c = TestClient(create_app(Settings(**READ), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3)))
        html = c.get("/datasets").text
        self.assertNotIn("Search by meaning or content", html)
        self.assertEqual(html.count('<tr class="dataset">'), 3)

    def test_config_pair_and_defaults(self):
        env = {"CRSW_S3_ENDPOINT": "https://rgw.example", "CRSW_S3_ACCESS_KEY": "t",
               "CRSW_S3_SECRET_KEY": "t", "CRSW_S3_BUCKET": "crsw",
               "CRSW_STAGING_PREFIX": "staging/_test", "CRSW_LLM_API_KEY": "k"}
        with self.assertRaises(ConfigError) as cm:
            Settings.from_env(env)
        self.assertIn("CRSW_LLM_BASE_URL", str(cm.exception))
        env["CRSW_LLM_BASE_URL"] = "https://ai.example/api/v1"
        s = Settings.from_env(env)
        self.assertEqual((s.llm_chat_model, s.llm_embed_model, s.llm_rerank_model,
                          s.llm_embed_dims, s.llm_timeout_seconds, s.llm_sensitivities),
                         ("arc:lite", "arc:embedvl", "arc:rerankvl", 1024, 20, ("green",)))
        self.assertFalse(s.llm_enabled)                  # no read role
        self.assertFalse(s.search_enabled)
        env.update({"CRSW_READ_S3_ACCESS_KEY": "r", "CRSW_READ_S3_SECRET_KEY": "r",
                    "CRSW_LLM_SENSITIVITIES": "green, amber", "CRSW_LLM_EMBED_DIMS": "0",
                    "CRSW_LLM_RERANK_MODEL": ""})
        s = Settings.from_env(env)
        self.assertTrue(s.llm_enabled)
        self.assertTrue(s.search_enabled)
        self.assertEqual((s.llm_sensitivities, s.llm_embed_dims, s.llm_rerank_model),
                         (("green", "amber"), 0, ""))
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(env, CRSW_LLM_SENSITIVITIES="green,red"))
        self.assertFalse(Settings.from_env(dict(env, CRSW_LLM_EMBED_MODEL="")).search_enabled)


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class SearchBase(PlatformBase):
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
        self.embed_store()
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

    def write_passages(self, identifiers=(CLEAN, GREEN), sensitivities=("green",),
                       model="toy"):
        import json
        for ident in identifiers:
            key = ident + "/dataset.%s.json" % ident.rsplit("/", 1)[-1]
            rec = json.loads(self.s3.get_object(Bucket="crsw", Key=key)["Body"].read())
            if rec["sensitivity"] not in sensitivities:
                continue
            pmod.refresh(self.s3, "crsw", PREFIX, ident, rec, FakePlatform(), model, None,
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

    @staticmethod
    def passages_part(html):
        """The page from the Passages heading on."""
        return html.split("<h2>Passages</h2>", 1)[1]


class TestOff(SearchBase):
    def test_on_with_the_embedding_model_alone(self):
        """The page ranks dataset descriptions from the embeddings file,
        so it is on whenever the embedding model is; the passages group
        needs CRSW_PASSAGES as well. No platform, or a blank embedding
        model, and the page and its link are gone."""
        self.write_passages()
        self.embed_store()
        c = TestClient(create_app(Settings(**LLM), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3), platform=FakePlatform()))
        self.assertEqual(c.get("/search", params={"q": "x"}).status_code, 200)
        self.assertEqual(c.get("/search.json", params={"q": "x"}).status_code, 200)
        self.assertIn('href="/search">Search by meaning or content</a>', c.get("/datasets").text)
        self.assertNotIn("<h2>Passages</h2>", c.get("/search", params={"q": "x"}).text)
        # Passages switched on but no platform: off, no link.
        c = TestClient(create_app(Settings(**dict(READ, passages=True)), s3_client=self.s3,
                                  vocab_dict=VOCAB, read_client=s3mod.ReadOnly(self.s3)))
        self.assertEqual(c.get("/search").status_code, 404)
        self.assertNotIn("Search by meaning or content", c.get("/datasets").text)
        # Embedding model blank: off, whatever else is set.
        c = TestClient(create_app(Settings(**dict(SEARCH, llm_embed_model="")), s3_client=self.s3,
                                  vocab_dict=VOCAB, read_client=s3mod.ReadOnly(self.s3),
                                  platform=FakePlatform(off=("embed",))))
        self.assertEqual(c.get("/search").status_code, 404)
        self.assertNotIn("Search by meaning or content", c.get("/datasets").text)
        s = Settings(**dict(SEARCH, llm_embed_model=""))
        self.assertFalse(s.search_enabled)
        self.assertFalse(s.passages_enabled)
        self.assertTrue(Settings(**SEARCH).passages_enabled)
        self.assertTrue(Settings(**LLM).search_enabled)
        self.assertFalse(Settings(**LLM).passages_enabled)
        # The platform without the read role: nothing to search.
        s = Settings(**dict(SETTINGS, llm_base_url="https://ai.example", llm_api_key="k"))
        self.assertFalse(s.llm_enabled)
        c = TestClient(create_app(s, s3_client=self.s3, vocab_dict=VOCAB))
        self.assertEqual(c.get("/search", params={"q": "x"}).status_code, 404)

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

    def test_stats_are_counted_once_per_sync(self):
        self.write_passages()
        ix = self.index()
        ix.sync()
        self.assertEqual(ix.stats(model="m")["passages"], 4)
        cursor = ix._cursor

        def no_query():
            raise AssertionError("stats queried the copy again")
        ix._cursor = no_query
        self.assertEqual(ix.stats(model="m")["passages"], 4)   # remembered
        self.assertEqual(ix.stats()["passages"], 4)
        ix._cursor = cursor
        self.put_member(CLEAN, "method.txt", b"Different words about deaths in war.\n")
        self.write_passages((CLEAN, AMBER), ("green", "amber"))
        ix.sync()
        self.assertEqual(ix.stats(model="m")["passages"], 5)   # a sync forgets

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
            def embed(self, texts, batch=32, instruction=None):
                return [v[:small] for v in super().embed(texts, batch, instruction)]
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

    def test_only_passages_of_the_given_fingerprint_are_searched(self):
        """Two datasets, one rebuilt for the new model and one not yet:
        a search keyed to the new fingerprint sees only the first, and
        the stats say how many passages are current."""
        self.write_passages((CLEAN,), model="toy")
        self.write_passages((GREEN,), model="other")
        ix = self.index()
        ix.sync()
        self.assertEqual(ix.stats(), {"passages": 4, "datasets": 2, "files": 4})
        self.assertEqual(ix.stats(model="toy")["current"], 1)
        self.assertEqual(ix.stats(model="other")["current"], 3)
        self.assertEqual(ix.stats(model="none")["current"], 0)
        hits = ix.search(bag("conflict deaths per year"), model="toy")
        self.assertEqual({h["identifier"] for h in hits}, {CLEAN})
        hits = ix.search(bag("conflict deaths per year"), model="other")
        self.assertEqual({h["identifier"] for h in hits}, {GREEN})
        # No fingerprint asked: every row (bar the two-word passage, under MIN_WORDS).
        self.assertEqual(len(ix.search(bag("conflict deaths per year"))), 3)

    def test_a_copy_without_the_model_column_is_rebuilt(self):
        """A copy made before vectors were keyed to a fingerprint has no
        `model` column: it is emptied and refilled from the bucket, as
        for a change of vector size."""
        self.write_passages()
        os.makedirs(os.path.dirname(self.path))
        con = duckdb.connect(self.path)
        con.execute("CREATE TABLE files (key VARCHAR PRIMARY KEY, etag VARCHAR)")
        con.execute("CREATE TABLE meta (name VARCHAR PRIMARY KEY, value VARCHAR)")
        con.execute("INSERT INTO meta VALUES ('dims', ?)", [str(DIM)])
        con.execute("CREATE TABLE passages (identifier VARCHAR, dataset_uuid VARCHAR, "
                    "sensitivity VARCHAR, member VARCHAR, checksum VARCHAR, page INTEGER, "
                    "position INTEGER, words INTEGER, text VARCHAR, dimension INTEGER, "
                    "embedding FLOAT[%d], key VARCHAR)" % DIM)
        con.execute("INSERT INTO files VALUES ('stale', 'etag')")
        con.close()
        ix = self.index(dims=DIM)
        s = ix.sync()
        self.assertEqual((s["loaded"], s["dropped"]), (2, 0))
        self.assertEqual(ix.stats(model="toy")["current"], 4)

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

    def test_a_load_that_fails_for_a_reason_other_than_the_file_is_tried_again(self):
        self.write_passages()
        ix = self.index(dims=DIM)
        calls = []
        real = ix._replace

        def flaky(key, table):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("disk full")          # not the file's fault
            return real(key, table)
        with mock.patch.object(ix, "_replace", flaky):
            s = ix.sync()
            self.assertEqual((s["loaded"], s["skipped"]), (1, 1))
            s = ix.sync()
        self.assertEqual((s["loaded"], s["skipped"], len(calls)), (1, 0, 3))
        self.assertEqual(ix.stats()["passages"], 4)

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
        self.assertIn('href="/search">Search by meaning or content</a>', html)
        for path in ("/", "/datasets/" + GREEN):
            self.assertIn('href="/search">Search by meaning or content</a>', c.get(path).text, path)
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
        # The question is a query and carries the model's instruction; the
        # sentences are documents, compared against it, and carry none.
        self.assertEqual(p.embed_instructions, [llm_mod.QUERY_INSTRUCTION, None])
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

    def test_query_instruction_can_be_switched_off(self):
        """A blank CRSW_LLM_QUERY_INSTRUCTION sends the question bare, as
        before, with nothing stored touched."""
        self.write_passages((CLEAN, GREEN))
        p = FakePlatform()
        body = self.hits(self.app(platform=p, llm_query_instruction=""), "conflict deaths per year")
        self.assertEqual(body["steps"], ["meaning", "rerank", "highlight"])
        # The blank reaches Platform.embed as it is, which sends the text
        # bare for None and blank alike (test_llm); nothing is instructed.
        self.assertEqual(len(p.embed_instructions), 2)
        self.assertFalse(any(p.embed_instructions))

    def test_passages_from_another_model_are_refused_with_a_notice(self):
        """The alias now resolves to another backend than the one that
        made the copy: no passage is scored, the page says so, and the
        question is not even embedded. Once some datasets are rebuilt,
        those are searched and the rest wait."""
        self.write_passages((CLEAN, GREEN))
        p = FakePlatform(fingerprint="other")
        clock = {"now": 0.0}
        ix = self.index(refresh_seconds=60, clock=lambda: clock["now"])
        c = self.app(platform=p, index=ix)
        body = self.hits(c, "conflict deaths per year")
        self.assertEqual((body["passages"], body["steps"]), ([], []))
        self.assertIn("different embedding model", body["notice"])
        self.assertEqual(p.embed_calls, [])
        self.assertIn("different embedding model", c.get("/search", params={"q": "x"}).text)
        # The dataset vectors are from the old backend too: both notices.
        self.assertIn("Ranking of descriptions by meaning is off", body["notice"])
        # One dataset rebuilt under the new fingerprint, seen on the next
        # cycle; the dataset vectors rebuilt too (a fresh catalogue reads them).
        self.write_passages((CLEAN,), model="other")
        self.embed_store(model="other")
        clock["now"] = 61
        c = self.app(platform=p, index=ix)
        body = self.hits(c, "conflict deaths per year")
        self.assertIsNone(body["notice"])
        self.assertEqual({h["identifier"] for h in body["passages"]}, {CLEAN})
        self.assertEqual(body["stats"]["current"], 1)

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
        self.assertIn("<h1>Search by meaning or content</h1>", html)
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
            def embed(self, texts, batch=32, instruction=None):
                if self.embed_calls:
                    self.embed_calls.append(list(texts))
                    raise PlatformError("platform returned HTTP 503 for /embeddings")
                return super().embed(texts, batch, instruction)
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

    def assert_all_folded(self, html, n):
        """No strong result: the page says so and folds everything, so a
        question the corpus has nothing for looks like one (the all-weak
        list was shown open until v0.15.1)."""
        html = self.passages_part(html)
        self.assertIn("Nothing contains your words, and nothing is close in meaning.", html)
        self.assertIn("<summary>%d passage%s, the closest in meaning</summary>"
                      % (n, "" if n == 1 else "s"), html)
        self.assertLess(html.index("<details"), html.index('<ol class="hits">'))

    def test_the_order_switch_is_labelled_in_plain_words(self):
        self.write_passages((CLEAN, GREEN))
        c = self.app(platform=FakePlatform())
        html = c.get("/search").text
        self.assertIn('value="words" checked> Exact matching text</label>', html)
        self.assertIn('value="meaning"> By close meaning</label>', html)
        self.assertNotIn("your words first", html)
        self.assertNotIn("by meaning alone", html)
        html = c.get("/search", params={"q": "zzzz", "sort": "meaning"}).text
        self.assertIn('value="meaning" checked> By close meaning</label>', html)
        self.assertIn('value="words"> Exact matching text</label>', html)

    def test_weak_meaning_only_results_are_folded(self):
        self.write_passages((CLEAN, GREEN))
        c = self.app(platform=FakePlatform())
        # Word matches present: every meaning-only result is weak and folded.
        body = self.hits(c, "populate automatically where possible")
        self.assertEqual([h["weak"] for h in body["passages"] if h["why"] != "meaning"], [False])
        self.assertTrue(all(h["weak"] for h in body["passages"] if h["why"] == "meaning"))
        html = self.passages_part(c.get("/search", params={"q": "populate automatically where possible"}).text)
        self.assertIn("<summary>2 more passages, close in meaning only</summary>", html)
        self.assertLess(html.index("contains your exact words"), html.index("<details"))
        self.assertNotIn("Nothing contains your words", html)
        # No word matches: strong needs both conditions, within the band
        # of the best AND at least the floor (weak-band-handoff.md); the
        # rest fold, and the page says what it is showing.
        body = self.hits(c, "zzzz")
        scores = [h["score"] for h in body["passages"]]
        best = max(scores)
        self.assertEqual([h["weak"] for h in body["passages"]],
                         [s < best - 0.10 or s < 0.35 for s in scores])
        html = c.get("/search", params={"q": "zzzz"}).text
        if all(h["weak"] for h in body["passages"]):
            self.assert_all_folded(html, len(body["passages"]))
        else:
            self.assertNotIn("Nothing contains your words", html)
        # The floor alone: a best below it is a poor best, everything folds.
        wide = self.app(platform=FakePlatform(), search_weak_band=1.0, search_weak_floor=0.0)
        body = self.hits(wide, "zzzz")
        self.assertTrue(body["passages"])
        self.assertFalse(any(h["weak"] for h in body["passages"]))
        floored = self.app(platform=FakePlatform(), search_weak_band=1.0,
                           search_weak_floor=best + 0.01)
        body = self.hits(floored, "zzzz")
        self.assertTrue(all(h["weak"] for h in body["passages"]))
        self.assert_all_folded(floored.get("/search", params={"q": "zzzz"}).text,
                               len(body["passages"]))
        # The band alone, with the floor off: only the best's neighbours stay.
        narrow = self.app(platform=FakePlatform(), search_weak_band=0.0, search_weak_floor=0.0)
        body = self.hits(narrow, "zzzz")
        self.assertEqual([h["weak"] for h in body["passages"]],
                         [s < best for s in scores])
        # Under sort=meaning the list is one list by similarity, so the
        # fold is the band and floor alone, measured from the top of that
        # list; a word match is never weak and no longer folds the rest.
        body = self.hits(c, "populate automatically where possible", sort="meaning")
        self.assertIn("phrase", [h["why"] for h in body["passages"]])
        top = max(h["score"] for h in body["passages"])
        self.assertEqual([h["weak"] for h in body["passages"]],
                         [h["why"] == "meaning" and (h["score"] < top - 0.10 or h["score"] < 0.35)
                          for h in body["passages"]])
        body = self.hits(wide, "populate automatically where possible", sort="meaning")
        self.assertIn("phrase", [h["why"] for h in body["passages"]])
        self.assertFalse(any(h["weak"] for h in body["passages"]))
        html = self.passages_part(wide.get("/search", params={"q": "populate automatically where possible",
                                                              "sort": "meaning"}).text)
        self.assertNotIn("<details", html)
        self.assertNotIn("Nothing contains your words", html)
        # Under the default order the words rule stands whatever the band.
        body = self.hits(wide, "populate automatically where possible")
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


class TestVectors(PlatformBase):
    """The catalogue's ranking by vector, which the search page's
    datasets group reads."""

    def test_a_vector_from_another_model_is_ignored(self):
        self.embed_store()
        cat = Catalogue(self.s3, "crsw")
        self.assertTrue(cat.has_vectors())
        self.assertEqual(cat.rank([1.0] * (DIM + 1)), [])
        ranked = cat.rank(bag("conflict deaths per year"))
        self.assertEqual(ranked[0][0], CLEAN)
        self.assertEqual([i for i, _ in ranked], [CLEAN, GREEN])   # amber has no vector
        self.assertEqual([i for i, _ in cat.rank(bag("x"), within={GREEN})], [GREEN])
        self.assertEqual(cat.rank(bag("x"), within=set()), [])

    def test_only_vectors_of_the_given_fingerprint_are_ranked(self):
        """A vector made by another model is never scored against the
        question: rows keyed to another fingerprint stay out of the
        ranking, and the catalogue can say how many are current."""
        self.embed_store()
        cat = Catalogue(self.s3, "crsw")
        self.assertEqual((cat.vectors_for("toy"), cat.vectors_for("other")), (2, 0))
        self.assertEqual(cat.rank(bag("conflict deaths per year"), model="other"), [])
        self.assertEqual([i for i, _ in cat.rank(bag("conflict deaths per year"), model="toy")],
                         [CLEAN, GREEN])
        self.rekey(GREEN, "other")
        cat = Catalogue(self.s3, "crsw")
        self.assertEqual((cat.vectors_for("toy"), cat.vectors_for("other")), (1, 1))
        self.assertEqual([i for i, _ in cat.rank(bag("x"), model="toy")], [CLEAN])
        self.assertEqual([i for i, _ in cat.rank(bag("x"), model="other")], [GREEN])
        self.assertEqual(len(cat.rank(bag("x"))), 2)            # no fingerprint: as before

    def test_ranking_during_a_refresh_uses_the_vectors_already_loaded(self):
        import threading
        self.embed_store()
        cat = Catalogue(self.s3, "crsw")
        self.assertTrue(cat.has_vectors())
        stop, problems = threading.Event(), []

        def refresh():
            try:
                for _ in range(30):
                    cat._load()
            except Exception as e:
                problems.append("refresh: %r" % e)
            finally:
                stop.set()

        t = threading.Thread(target=refresh, daemon=True)
        t.start()
        while not stop.is_set():
            try:
                if [i for i, _ in cat.rank(bag("conflict deaths per year"))] != [CLEAN, GREEN]:
                    problems.append("ranked without the loaded vectors")
                if not cat.has_vectors():
                    problems.append("vectors gone mid-refresh")
            except Exception as e:
                problems.append("rank: %r" % e)
        t.join(30)
        self.assertEqual(problems[:3], [])


class TestRankDatasets(PlatformBase):
    """`Catalogue.rank_datasets`: the three tiers passages have (exact
    words, all the words, meaning), over a dataset's name and abstract,
    with the vector's cosine where there is one."""

    def test_tiers_and_order(self):
        self.embed_store()
        cat = Catalogue(self.s3, "crsw")
        q = "conflict deaths per year"
        rows = cat.rank_datasets(q, bag(q), model="toy", stems=question_stems(q))
        self.assertEqual([(r["identifier"], r["why"]) for r in rows],
                         [(CLEAN, "phrase"), (GREEN, "meaning")])
        self.assertTrue(all(isinstance(r["score"], float) for r in rows))
        # A name typed finds the dataset, no vector needed; the score is then none.
        rows = cat.rank_datasets("interviews", None, model="toy",
                                 stems=question_stems("interviews"))
        self.assertEqual([(r["identifier"], r["why"], r["score"]) for r in rows],
                         [(AMBER, "phrase", None)])
        # Every telling word, in any order, is the second tier.
        rows = cat.rank_datasets("database CSAC", None, stems=question_stems("database CSAC"))
        self.assertEqual([(r["identifier"], r["why"]) for r in rows], [(CLEAN, "words")])
        # Nothing in the words: by meaning, and only rows with a current vector.
        rows = cat.rank_datasets("zzzz", bag("zzzz"), model="toy", stems=[])
        self.assertEqual(sorted(r["identifier"] for r in rows), sorted([CLEAN, GREEN]))
        self.assertTrue(all(r["why"] == "meaning" for r in rows))
        self.assertEqual(cat.rank_datasets("zzzz", bag("zzzz"), model="other", stems=[]), [])
        self.assertEqual(len(cat.rank_datasets("zzzz", bag("zzzz"), model="toy", stems=[], limit=1)), 1)
        # By meaning alone: one list by score; a row with no vector cannot be placed.
        q = "cleaned CSAC"
        rows = cat.rank_datasets(q, bag("x"), model="toy", stems=question_stems(q), by_meaning=True)
        self.assertNotIn(AMBER, [r["identifier"] for r in rows])
        self.assertEqual([r["why"] for r in rows], ["words", "words"])
        self.assertEqual([r["score"] for r in rows], sorted((r["score"] for r in rows), reverse=True))


class TestDatasetsGroup(SearchBase):
    def test_datasets_ranked_beside_passages(self):
        self.write_passages((CLEAN, GREEN))
        p = FakePlatform()
        c = self.app(platform=p)
        body = self.hits(c, "conflict deaths per year")
        self.assertEqual([(d["identifier"], d["why"], d["weak"]) for d in body["datasets"]],
                         [(CLEAN, "phrase", False), (GREEN, "meaning", True)])
        d = body["datasets"][0]
        self.assertEqual((d["dataset"], d["strand"], d["project"], d["sensitivity"]),
                         ("csac-clean", "rs2", "csac", "green"))
        self.assertIn("Conflict deaths per year", d["abstract"])
        self.assertTrue(all(s["how"] == "phrase" for s in d["sentences"]))
        self.assertIsInstance(d["score"], float)
        # The meaning-only dataset's sentences went to the platform in the
        # one highlighting call with the passages', and one is marked.
        self.assertEqual(len(p.embed_calls), 2)
        self.assertIn("Events from the CSAC extract, cleaned.", p.embed_calls[-1])
        self.assertTrue(any(s["how"] == "meaning" for s in body["datasets"][1]["sentences"]))
        self.assertEqual(body["stats"]["described"], 2)
        self.assertTrue(body["passages"])
        html = c.get("/search", params={"q": "conflict deaths per year"}).text
        self.assertIn("2 dataset descriptions", html)
        self.assertLess(html.index("<h2>Datasets</h2>"), html.index("<h2>Passages</h2>"))
        self.assertIn('<a href="/datasets/%s">csac-clean</a><span class="tag strand">rs2</span>' % CLEAN, html)
        self.assertIn('<button type="button" class="link why-ask" data-identifier="%s">Why this?</button>' % CLEAN, html)
        self.assertIn("<summary>1 more dataset, close in meaning only</summary>", html)
        self.assertIn('<mark class="phrase" title="contains your exact words"><b>Conflict</b> <b>deaths</b> per <b>year</b>, cleaned from the CSAC database.</mark>', html)
        # Without passages the group is the whole page.
        c = TestClient(create_app(Settings(**LLM), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3), platform=FakePlatform()))
        body = self.hits(c, "conflict deaths per year")
        self.assertEqual([d["identifier"] for d in body["datasets"]], [CLEAN, GREEN])
        self.assertEqual(body["passages"], [])
        self.assertNotIn("passages", body["stats"])
        html = c.get("/search", params={"q": "conflict deaths per year"}).text
        self.assertIn("<h2>Datasets</h2>", html)
        self.assertNotIn("<h2>Passages</h2>", html)

    def test_meaning_only_datasets_are_reranked_and_amber_sent_only_when_allowed(self):
        self.embed_store(include_amber=True)
        p = FakePlatform()
        c = self.app(platform=p)
        body = self.hits(c, "zzzz")
        ids = [d["identifier"] for d in body["datasets"]]
        self.assertEqual(sorted(ids), sorted([CLEAN, GREEN, AMBER]))   # listed for everyone
        self.assertIn("rerank", body["steps"])
        docs = [d for _, docs in p.rerank_calls for d in docs]
        self.assertTrue(any(d.startswith("csac-clean. Conflict deaths") for d in docs))
        self.assertFalse(any("Interview" in d for d in docs))
        p2 = FakePlatform()
        self.app(platform=p2, llm_sensitivities=("green", "amber")).get("/search.json", params={"q": "zzzz"})
        self.assertTrue(any("Interview" in d for _, docs in p2.rerank_calls for d in docs))
        # The reranker's order wins among what it was sent; the amber row,
        # not sent, keeps its place after them.
        rev = [d["identifier"] for d in self.hits(self.app(platform=FakePlatform(reverse=True)), "zzzz")["datasets"]]
        green = [i for i in ids if i != AMBER]
        self.assertEqual([i for i in rev if i != AMBER], green[::-1])
        self.assertEqual(rev.index(AMBER), ids.index(AMBER))
        # By meaning alone: one list by score, the reranker not asked.
        p3 = FakePlatform(reverse=True)
        body = self.hits(self.app(platform=p3), "zzzz", sort="meaning")
        scores = [d["score"] for d in body["datasets"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(p3.rerank_calls, [])

    def test_without_current_vectors_descriptions_match_by_words_only(self):
        # The alias repointed: nothing ranked by meaning, the words still
        # match, the question is not embedded, and the page says why.
        c = self.app(platform=FakePlatform(fingerprint="other"))
        body = self.hits(c, "conflict deaths")
        self.assertEqual([d["identifier"] for d in body["datasets"]], [CLEAN])
        self.assertEqual(body["steps"], [])
        self.assertIn("Ranking of descriptions by meaning is off until the index is rebuilt "
                      "for the current embedding model", body["notice"])
        self.assertEqual(body["stats"]["described"], 0)
        html = c.get("/search", params={"q": "qqqq"}).text
        self.assertIn("Ranking of descriptions by meaning is off", html)
        self.assertIn("No description is close enough.", html)
        # Half way through a rebuild: what is current ranks, the rest waits.
        self.rekey(GREEN, "other")
        body = self.hits(self.app(platform=FakePlatform()), "zzzz")
        self.assertEqual([d["identifier"] for d in body["datasets"]], [CLEAN])
        self.assertIsNone(body["notice"])
        self.assertEqual(body["steps"], ["meaning", "highlight"])

    def test_embedding_down_leaves_the_words(self):
        self.write_passages()
        body = self.hits(self.app(platform=FakePlatform(fail=("embed",))), "conflict deaths")
        self.assertEqual([(d["identifier"], d["why"]) for d in body["datasets"]], [(CLEAN, "phrase")])
        self.assertEqual(body["passages"], [])
        self.assertIn("unavailable", body["notice"])

    def test_why_this_for_a_dataset(self):
        p = FakePlatform()
        c = self.app(platform=p)
        r = c.get("/search/why", params={"q": "conflict deaths", "identifier": CLEAN})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"why": "It mentions Conflict.", "notice": None})
        self.assertEqual(p.explain_calls[0][0], "conflict deaths")
        self.assertIn("Conflict deaths per year", p.explain_calls[0][1])
        # Amber: its abstract may not be sent until amber is allowed.
        self.assertEqual(c.get("/search/why", params={"q": "x", "identifier": AMBER}).status_code, 403)
        c2 = self.app(platform=FakePlatform(), llm_sensitivities=("green", "amber"))
        self.assertEqual(c2.get("/search/why", params={"q": "x", "identifier": AMBER}).status_code, 200)
        self.assertEqual(c.get("/search/why", params={"q": "x", "identifier": "rs9/no/green/0_raw/none"}).status_code, 404)
        # The button follows the same rule.
        self.embed_store(include_amber=True)
        html = self.app(platform=FakePlatform()).get("/search", params={"q": "zzzz"}).text
        self.assertNotIn('data-identifier="%s"' % AMBER, html)
        html = self.app(platform=FakePlatform(), llm_sensitivities=("green", "amber")).get(
            "/search", params={"q": "zzzz"}).text
        self.assertIn('class="link why-ask" data-identifier="%s">Why this?' % AMBER, html)
        # Without passages the route still answers for a dataset.
        c3 = TestClient(create_app(Settings(**LLM), s3_client=self.s3, vocab_dict=VOCAB,
                                   read_client=s3mod.ReadOnly(self.s3), platform=FakePlatform()))
        self.assertEqual(c3.get("/search/why", params={"q": "x", "identifier": CLEAN}).status_code, 200)

    def test_weak_descriptions_fold_on_their_own(self):
        c = self.app(platform=FakePlatform(), search_weak_band=1.0, search_weak_floor=2.0)
        body = self.hits(c, "zzzz")
        self.assertTrue(body["datasets"])
        self.assertTrue(all(d["weak"] for d in body["datasets"]))
        html = c.get("/search", params={"q": "zzzz"}).text
        self.assertIn("No description contains your words, and none is close in meaning.", html)
        self.assertIn("<summary>2 datasets, the closest in meaning</summary>", html)
        wide = self.app(platform=FakePlatform(), search_weak_band=1.0, search_weak_floor=0.0)
        self.assertFalse(any(d["weak"] for d in self.hits(wide, "zzzz")["datasets"]))
        html = wide.get("/search", params={"q": "zzzz"}).text
        self.assertNotIn("No description contains your words", html)
        self.assertIn("<p class=\"count\">2 datasets</p>", html)
