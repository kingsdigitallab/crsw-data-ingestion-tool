"""The platform client: one POST per call, batching, strict parsing,
every failure a PlatformError. No network: httpx.MockTransport."""
import json
import unittest

try:
    import httpx
    from crsw_web import llm
    HAVE = True
except ImportError:            # pragma: no cover
    HAVE = False


def fake_platform(handler):
    """A Platform whose HTTP goes to `handler(request) -> httpx.Response`."""
    return llm.Platform("https://ai.example/api/v1", "sk-test",
                        transport=httpx.MockTransport(handler))


@unittest.skipUnless(HAVE, "web extras not installed")
class TestEmbed(unittest.TestCase):
    def test_batches_in_order_with_the_bearer_key(self):
        seen = []

        def handler(request):
            seen.append(request)
            body = json.loads(request.content)
            data = [{"index": i, "embedding": [float(len(t)), 1.0]}
                    for i, t in enumerate(body["input"])]
            data.reverse()                      # out of order on purpose
            return httpx.Response(200, json={"data": data, "model": body["model"]})

        p = fake_platform(handler)
        texts = ["a" * n for n in range(1, 71)]
        vectors = p.embed(texts)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0].url.path, "/api/v1/embeddings")
        self.assertEqual(seen[0].headers["authorization"], "Bearer sk-test")
        self.assertEqual(json.loads(seen[0].content)["model"], "arc:embedvl")
        self.assertEqual([v[0] for v in vectors], [float(n) for n in range(1, 71)])
        self.assertEqual(p.embed([]), [])

    def test_failures_are_platform_errors(self):
        p = fake_platform(lambda r: httpx.Response(503, text="down"))
        with self.assertRaises(llm.PlatformError) as cm:
            p.embed(["x"])
        self.assertIn("HTTP 503", str(cm.exception))
        p = fake_platform(lambda r: httpx.Response(200, json={"data": []}))
        with self.assertRaises(llm.PlatformError):
            p.embed(["x"])

        def boom(request):
            raise httpx.ConnectError("no route")
        with self.assertRaises(llm.PlatformError) as cm:
            fake_platform(boom).embed(["x"])
        self.assertIn("unreachable", str(cm.exception))

    def test_dims_cut_and_rescaled(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0, 9.0, 9.0]}]})
        p = llm.Platform("https://ai.example/api/v1", "k", embed_dims=2,
                         transport=httpx.MockTransport(handler))
        self.assertEqual(p.embed(["x"]), [[0.6, 0.8]])
        p = llm.Platform("https://ai.example/api/v1", "k", embed_dims=8,
                         transport=httpx.MockTransport(handler))
        self.assertEqual(p.embed(["x"]), [[3.0, 4.0, 9.0, 9.0]])    # shorter: untouched

    def test_blank_model_names_are_kept_blank(self):
        p = llm.Platform("https://ai.example", "k", chat_model="", rerank_model=None)
        self.assertEqual((p.chat_model, p.rerank_model, p.embed_model), ("", "", "arc:embedvl"))
        p.close()

    def test_from_settings_is_off_without_url_and_key(self):
        class S:
            llm_base_url = ""
            llm_api_key = ""
        self.assertIsNone(llm.Platform.from_settings(S()))
        S.llm_base_url, S.llm_api_key = "https://ai.example/api/v1/", "k"
        S.llm_embed_model = "other:model"
        S.llm_embed_dims = 512
        p = llm.Platform.from_settings(S())
        self.assertEqual((p.base_url, p.embed_model, p.chat_model, p.embed_dims),
                         ("https://ai.example/api/v1", "other:model", "arc:lite", 512))
        p.close()


@unittest.skipUnless(HAVE, "web extras not installed")
class TestFilterFor(unittest.TestCase):
    ARGS = dict(strands=("rs1", "rs2"), states=("0_raw", "2_final"),
                sensitivities=("green", "amber"),
                subjects=("armed-conflict", "forced-labour"), projects=("csac",))

    def reply(self, content):
        def handler(request):
            self.request = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return fake_platform(handler)

    def test_values_outside_the_vocabulary_are_dropped(self):
        p = self.reply('<think>hmm</think>```json\n{"strand": "rs2", "state": "9_x", '
                       '"subject": ["armed-conflict"], "project": "nope", '
                       '"words": "one two three four five six seven", '
                       '"year_from": "1990", "year_to": "nineteen"}\n```')
        f = p.filter_for("deaths in conflicts since 1990", **self.ARGS)
        self.assertEqual(f, {"strand": "rs2", "subject": "armed-conflict",
                             "words": "one two three four five six", "year_from": "1990"})
        self.assertEqual(self.request["model"], "arc:lite")
        self.assertEqual(self.request["temperature"], 0)
        system = self.request["messages"][0]["content"]
        self.assertIn("armed-conflict, forced-labour", system)
        self.assertIn("csac", system)
        self.assertEqual(self.request["messages"][1]["content"], "deaths in conflicts since 1990")

    def test_prose_around_the_object_and_no_object(self):
        p = self.reply('Sure. Here it is: {"sensitivity": "green"} Hope that helps.')
        self.assertEqual(p.filter_for("q", **self.ARGS), {"sensitivity": "green"})
        p = self.reply("I do not know.")
        self.assertEqual(p.filter_for("q", **self.ARGS), {})
        p = self.reply("[1, 2]")
        self.assertEqual(p.filter_for("q", **self.ARGS), {})

    def test_missing_message_is_an_error(self):
        p = fake_platform(lambda r: httpx.Response(200, json={"choices": []}))
        with self.assertRaises(llm.PlatformError):
            p.filter_for("q", **self.ARGS)


@unittest.skipUnless(HAVE, "web extras not installed")
class TestExplain(unittest.TestCase):
    def test_one_sentence_from_the_chat_model(self):
        def handler(request):
            self.request = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "<think>x</think>  The passage describes  how sizes are checked.\n"}}]})
        p = fake_platform(handler)
        self.assertEqual(p.explain("how are uploads verified", "Sizes are compared."),
                         "The passage describes how sizes are checked.")
        self.assertEqual(self.request["model"], "arc:lite")
        self.assertIn("Asked: how are uploads verified", self.request["messages"][1]["content"])
        self.assertIn("Sizes are compared.", self.request["messages"][1]["content"])
        with self.assertRaises(llm.PlatformError):
            fake_platform(lambda r: httpx.Response(500)).explain("q", "p")


@unittest.skipUnless(HAVE, "web extras not installed")
class TestRerank(unittest.TestCase):
    def test_orders_by_score_and_fills_in_the_rest(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["query"], "q")
            self.assertEqual(body["top_n"], 4)
            return httpx.Response(200, json={"results": [
                {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4},
                {"index": 7, "relevance_score": 1.0}]})       # 7 is out of range
        p = fake_platform(handler)
        self.assertEqual(p.rerank("q", ["a", "b", "c", "d"]), [2, 0, 1, 3])
        self.assertEqual(p.rerank("q", []), [])

    def test_bad_reply_is_an_error(self):
        p = fake_platform(lambda r: httpx.Response(404, text="no such route"))
        with self.assertRaises(llm.PlatformError):
            p.rerank("q", ["a"])


@unittest.skipUnless(HAVE, "web extras not installed")
class TestMalformedReplies(unittest.TestCase):
    """A reply of the wrong shape is a PlatformError like any other
    failure, never a TypeError that escapes the callers' fallbacks."""

    def check(self, call, body):
        p = fake_platform(lambda r: httpx.Response(200, json=body))
        try:
            call(p)
        except llm.PlatformError:
            return
        except Exception as e:
            self.fail("%r for reply %r" % (e, body))

    def test_embed(self):
        for data in ([None], [3.0], [{"index": None, "embedding": [1.0]}],
                     [{"index": 0, "embedding": [None]}], [{"index": 0, "embedding": ["x"]}],
                     [{"index": "0", "embedding": [1.0]}]):
            with self.subTest(data=data):
                self.check(lambda p: p.embed(["x"]), {"data": data})

    def test_rerank(self):
        for results in ([None], [0.5], [{"index": 0, "relevance_score": None}],
                        [{"index": 0, "relevance_score": "high"}],
                        [{"index": 0, "relevance_score": [1]}]):
            with self.subTest(results=results):
                self.check(lambda p: p.rerank("q", ["a", "b"]), {"results": results})

    def test_chat(self):
        for content in (123, ["a"], {"a": 1}):
            body = {"choices": [{"message": {"content": content}}]}
            with self.subTest(content=content):
                self.check(lambda p: p.explain("q", "passage"), body)
                self.check(lambda p: p.filter_for("q", ["rs1"], ["2_final"], ["green"], [], []),
                           body)

    def test_the_filter_shape_is_still_tolerant(self):
        body = {"choices": [{"message": {"content": '{"strand": ["rs1"], "words": 5}'}}]}
        p = fake_platform(lambda r: httpx.Response(200, json=body))
        self.assertEqual(p.filter_for("q", ["rs1"], ["2_final"], ["green"], [], []),
                         {"strand": "rs1", "words": "5"})


@unittest.skipUnless(HAVE, "web extras not installed")
class TestParseJsonObject(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(llm.parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(llm.parse_json_object('```\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(llm.parse_json_object('x {"a": {"b": 2}} y'), {"a": {"b": 2}})
        self.assertIsNone(llm.parse_json_object("nothing"))
        self.assertIsNone(llm.parse_json_object("{broken"))
