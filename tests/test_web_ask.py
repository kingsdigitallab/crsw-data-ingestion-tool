"""Asking the store a question (finding-and-reuse.md §5): filter, rank
by meaning, rerank, each step degrading on its own; amber abstracts
never sent unless allowed; the ordinary search untouched. The platform
is a fake with the same three methods."""
import unittest

from tests.test_web_catalogue import (HAVE_WEB, CatalogueBase, META, GREEN, AMBER,
                                      READ, SETTINGS, VOCAB)

if HAVE_WEB:
    from fastapi.testclient import TestClient
    from crsw_deposit import record
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.catalogue import Catalogue, describe_filter, words_match, years_overlap
    from crsw_web.config import ConfigError, Settings
    from crsw_web.llm import PlatformError
    from promoter import embed as embed_mod
    from promoter import index as index_mod

CLEAN = "rs2/csac/green/2_final/csac-clean"
ASK = dict(READ, llm_base_url="https://ai.example/api/v1", llm_api_key="sk-test")
DIM = 8


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

    def __init__(self, filter_reply=None, fail=(), reverse=False, off=()):
        self.filter_reply = filter_reply or {}
        self.fail = set(fail)
        self.reverse = reverse              # a reranker that disagrees, so it shows
        for name in off:                    # a blank model name is the off switch
            setattr(self, name + "_model", "")
        self.filter_calls, self.embed_calls, self.rerank_calls = [], [], []

    def filter_for(self, question, strands, states, sensitivities, subjects, projects):
        self.filter_calls.append((question, tuple(subjects), tuple(projects)))
        if "filter" in self.fail:
            raise PlatformError("platform returned HTTP 503 for /chat/completions")
        return dict(self.filter_reply)

    def embed(self, texts, batch=32):
        self.embed_calls.append(list(texts))
        if "embed" in self.fail:
            raise PlatformError("platform unreachable: ConnectError")
        return [bag(t) for t in texts]

    def explain(self, question, passage):
        self.explain_calls = getattr(self, "explain_calls", [])
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
class AskBase(CatalogueBase):
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

    def embed_store(self, include_amber=False):
        embed_mod.refresh(self.s3, "crsw", "index", self.index_rows, FakePlatform(),
                          "toy", {"green", "amber"} if include_amber else {"green"})

    def app(self, platform=None, **overrides):
        settings = Settings(**dict(ASK, **overrides))
        return TestClient(create_app(settings, s3_client=self.s3, vocab_dict=VOCAB,
                                     read_client=s3mod.ReadOnly(self.s3), platform=platform))

    def ids(self, client, **params):
        return [d["identifier"] for d in client.get("/datasets.json", params=params).json()["datasets"]]


class TestAskOff(AskBase):
    def test_no_platform_means_no_box_and_ask_is_ignored(self):
        c = TestClient(create_app(Settings(**READ), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3)))
        html = c.get("/datasets", params={"ask": "conflict deaths"}).text
        self.assertNotIn('id="ask"', html)
        self.assertNotIn("Understood as", html)
        self.assertEqual(html.count("<tr>"), 4)          # header + all three rows
        body = c.get("/datasets.json", params={"ask": "conflict deaths"}).json()
        self.assertNotIn("understood", body)
        self.assertEqual(len(body["datasets"]), 3)

    def test_platform_without_read_role_is_off(self):
        s = Settings(**dict(SETTINGS, llm_base_url="https://ai.example", llm_api_key="k"))
        self.assertFalse(s.ask_enabled)
        c = TestClient(create_app(s, s3_client=self.s3, vocab_dict=VOCAB))
        self.assertEqual(c.get("/datasets", params={"ask": "x"}).status_code, 404)

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
        self.assertFalse(s.ask_enabled)                  # no read role
        env.update({"CRSW_READ_S3_ACCESS_KEY": "r", "CRSW_READ_S3_SECRET_KEY": "r",
                    "CRSW_LLM_SENSITIVITIES": "green, amber", "CRSW_LLM_EMBED_DIMS": "0",
                    "CRSW_LLM_RERANK_MODEL": ""})
        s = Settings.from_env(env)
        self.assertTrue(s.ask_enabled)
        self.assertEqual((s.llm_sensitivities, s.llm_embed_dims, s.llm_rerank_model),
                         (("green", "amber"), 0, ""))
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(env, CRSW_LLM_SENSITIVITIES="green,red"))


class TestAsk(AskBase):
    def test_filter_then_meaning_then_rerank(self):
        self.embed_store()
        p = FakePlatform(filter_reply={"strand": "rs2", "subject": "armed-conflict",
                                       "year_from": "1990", "words": "conflict deaths"})
        c = self.app(platform=p)
        r = c.get("/datasets", params={"ask": "  anything on conflict deaths since 1990 "})
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn("Understood as: strand rs2, subject armed-conflict, from 1990, "
                      "words: conflict deaths.", html)
        # The filter it chose is the filled-in form.
        self.assertIn('value="conflict deaths"', html)
        self.assertIn('<option value="rs2" selected>', html)
        self.assertIn('<option value="armed-conflict" selected>', html)
        self.assertIn('id="ask" name="ask" type="search" value="anything on conflict deaths since 1990"', html)
        self.assertEqual(html.count("<tr>"), 2)          # header + the one match
        self.assertIn(CLEAN, html)
        # What the platform saw: the question, the vocabulary in use on
        # green rows, the question again for the vector; the reranker got
        # fewer than two documents so was not called.
        self.assertEqual(p.filter_calls[0][0], "anything on conflict deaths since 1990")
        self.assertEqual(p.filter_calls[0][1], ("armed-conflict", "forced-labour"))
        self.assertEqual(p.filter_calls[0][2], ("csac",))
        self.assertEqual(p.embed_calls, [["anything on conflict deaths since 1990"]])
        self.assertEqual(p.rerank_calls, [])
        body = c.get("/datasets.json", params={"ask": "conflict deaths since 1990"}).json()
        self.assertEqual([d["identifier"] for d in body["datasets"]], [CLEAN])
        self.assertEqual(body["steps"], ["filter", "meaning"])
        self.assertEqual(body["filter"]["strand"], "rs2")
        self.assertIsNone(body["notice"])

    def test_meaning_orders_green_rows_and_amber_falls_through_to_words(self):
        self.embed_store()
        p = FakePlatform()                                   # no filter at all
        c = self.app(platform=p)
        # The toy vectors: the question shares words with the clean abstract.
        ids = self.ids(c, ask="conflict deaths per year")
        self.assertEqual(ids[:2], [CLEAN, GREEN])           # ranked, both green
        self.assertNotIn(AMBER, ids)                        # no vector, no word match
        ids = self.ids(c, ask="interview transcripts")
        self.assertIn(AMBER, ids)                           # words match, no vector needed
        self.assertEqual(ids[0], AMBER)                     # and named in the question
        # Two green candidates: the reranker read them, and never the amber
        # abstract, in a form that is title then abstract.
        q, docs = p.rerank_calls[0]
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(d.startswith(("csac-clean. ", "events. ")) for d in docs))
        self.assertFalse(any("Interview" in d for d in docs))
        body = c.get("/datasets.json", params={"ask": "conflict deaths per year"}).json()
        self.assertEqual(body["steps"], ["filter", "meaning", "rerank"])
        self.assertEqual(body["understood"], "no filters; matching on the question itself")
        # The reranker's order wins over the vector order.
        c = self.app(platform=FakePlatform(reverse=True))
        self.assertEqual(self.ids(c, ask="conflict deaths per year")[:2], [GREEN, CLEAN])

    def test_amber_abstract_sent_only_when_allowed(self):
        self.embed_store(include_amber=True)
        p = FakePlatform()
        c = self.app(platform=p)
        self.ids(c, ask="transcripts of interviews with survivors")
        self.assertFalse(any("Interview" in d for _, docs in p.rerank_calls for d in docs))
        p = FakePlatform()
        c = self.app(platform=p, llm_sensitivities=("green", "amber"))
        ids = self.ids(c, ask="transcripts of interviews with survivors")
        self.assertIn(AMBER, ids)
        self.assertTrue(any("Interview" in d for _, docs in p.rerank_calls for d in docs))

    def test_amber_only_names_sent_only_when_allowed(self):
        """The filter prompt lists projects and subject terms; those found
        only on amber datasets stay out of it unless amber is allowed."""
        p = FakePlatform()
        self.ids(self.app(platform=p), ask="interviews")
        _, subjects, projects = p.filter_calls[0]
        self.assertNotIn("oral", projects)
        self.assertNotIn("forced-marriage", subjects)
        self.assertIn("csac", projects)
        p = FakePlatform()
        self.ids(self.app(platform=p, llm_sensitivities=("green", "amber")), ask="interviews")
        _, subjects, projects = p.filter_calls[0]
        self.assertIn("oral", projects)
        self.assertIn("forced-marriage", subjects)

    def test_each_step_degrades_on_its_own(self):
        self.embed_store()
        # Filter down: words of the question, ranking still by meaning.
        p = FakePlatform(fail=("filter",))
        body = self.app(platform=p).get("/datasets.json",
                                        params={"ask": "conflict deaths per year"}).json()
        self.assertEqual(body["steps"], ["meaning", "rerank"])
        self.assertIn("could not be interpreted", body["notice"])
        self.assertIn("HTTP 503", body["notice"])
        self.assertEqual(body["datasets"][0]["identifier"], CLEAN)
        # Embedding down: the filter and the words carry it.
        p = FakePlatform(filter_reply={"words": "conflict deaths"}, fail=("embed",))
        body = self.app(platform=p).get("/datasets.json", params={"ask": "q"}).json()
        self.assertEqual(body["steps"], ["filter"])
        self.assertIn("Ranking by meaning is unavailable", body["notice"])
        self.assertEqual([d["identifier"] for d in body["datasets"]], [CLEAN])
        # Reranker down: order kept, a notice, still a page not an error.
        p = FakePlatform(fail=("rerank",))
        r = self.app(platform=p).get("/datasets", params={"ask": "conflict deaths per year"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("reranker is unavailable", r.text)
        # Everything down: the question's words alone, and no crash.
        p = FakePlatform(fail=("filter", "embed", "rerank"))
        body = self.app(platform=p).get("/datasets.json", params={"ask": "cleaned"}).json()
        self.assertEqual(body["steps"], [])
        self.assertEqual(sorted(d["identifier"] for d in body["datasets"]), sorted([CLEAN, GREEN]))

    def test_each_function_switches_off_by_a_blank_model_name(self):
        self.embed_store()
        p = FakePlatform(filter_reply={"words": "conflict"}, off=("chat",))
        body = self.app(platform=p).get("/datasets.json", params={"ask": "conflict deaths"}).json()
        self.assertEqual(body["steps"], ["meaning", "rerank"])
        self.assertEqual(p.filter_calls, [])
        self.assertIsNone(body["notice"])                # off is not a failure
        p = FakePlatform(off=("embed",))          # "csac" matches two green rows by words
        body = self.app(platform=p).get("/datasets.json", params={"ask": "csac data"}).json()
        self.assertEqual(body["steps"], ["filter", "rerank"])
        self.assertEqual(p.embed_calls, [])
        p = FakePlatform(off=("rerank",))
        body = self.app(platform=p).get("/datasets.json", params={"ask": "conflict deaths"}).json()
        self.assertEqual(body["steps"], ["filter", "meaning"])
        self.assertEqual(p.rerank_calls, [])
        p = FakePlatform(off=("chat", "embed", "rerank"))
        body = self.app(platform=p).get("/datasets.json", params={"ask": "cleaned"}).json()
        self.assertEqual(body["steps"], [])
        self.assertEqual(len(body["datasets"]), 2)      # the words still match

    def test_without_an_embeddings_file(self):
        p = FakePlatform(filter_reply={"sensitivity": "amber"})
        body = self.app(platform=p).get("/datasets.json", params={"ask": "amber things"}).json()
        self.assertEqual(body["steps"], ["filter"])         # no vectors: no meaning step
        self.assertEqual(p.embed_calls, [])
        self.assertEqual([d["identifier"] for d in body["datasets"]], [AMBER])   # filter alone
        self.assertIsNone(body["notice"])

    def test_a_dataset_named_in_the_question_comes_first(self):
        self.embed_store()
        # The vectors and the reranker both prefer the clean dataset for
        # this question; the name wins anyway.
        p = FakePlatform()
        ids = self.ids(self.app(platform=p), ask="conflict deaths per year in events")
        self.assertEqual(ids[0], GREEN)                      # dataset "events"
        self.assertEqual(ids[1], CLEAN)

    def test_a_filter_that_matches_nothing_relaxes_with_a_notice(self):
        self.embed_store()
        p = FakePlatform(filter_reply={"strand": "rs4", "subject": "armed-conflict"})
        body = self.app(platform=p).get("/datasets.json",
                                        params={"ask": "conflict deaths per year"}).json()
        self.assertEqual(body["datasets"][0]["identifier"], CLEAN)
        self.assertIn("Nothing matches all of that", body["notice"])
        self.assertEqual(body["understood"], "strand rs4, subject armed-conflict")
        self.assertEqual(body["steps"], ["filter", "meaning", "rerank"])
        html = self.app(platform=p).get("/datasets", params={"ask": "conflict deaths"}).text
        self.assertIn("Understood as: strand rs4, subject armed-conflict.", html)
        self.assertIn("Nothing matches all of that", html)

    def test_blank_question_is_the_ordinary_search(self):
        p = FakePlatform()
        c = self.app(platform=p)
        html = c.get("/datasets", params={"ask": "   ", "q": "events"}).text
        self.assertIn('id="ask"', html)
        self.assertNotIn("Understood as", html)
        self.assertIn(GREEN, html)
        self.assertEqual(p.filter_calls, [])
        self.assertEqual(self.ids(c, q="events"), [GREEN])

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


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestHelpers(unittest.TestCase):
    def test_describe_filter(self):
        self.assertEqual(describe_filter({}), "no filters; matching on the question itself")
        self.assertEqual(describe_filter({"strand": "rs1", "year_to": "1950", "words": "ships"}),
                         "strand rs1, up to 1950, words: ships")
        self.assertEqual(describe_filter({"year_from": "1800", "year_to": "1900"}),
                         "years 1800 to 1900")

    def test_years_overlap(self):
        row = {"temporal_start": "1989", "temporal_end": "2025-12-31"}
        self.assertTrue(years_overlap(row, "1990", None))
        self.assertTrue(years_overlap(row, None, "1989"))
        self.assertFalse(years_overlap(row, "2030", None))
        self.assertFalse(years_overlap(row, None, "1900"))
        self.assertTrue(years_overlap({}, "1900", "1950"))

    def test_words_match(self):
        row = {"dataset": "csac-clean", "abstract": "Conflict deaths per year", "subject": []}
        self.assertTrue(words_match(row, "deaths conflict", all_of=True))
        self.assertFalse(words_match(row, "deaths births", all_of=True))
        self.assertTrue(words_match(row, "any births deaths", all_of=False))
        self.assertFalse(words_match(row, "a an the", all_of=False))
