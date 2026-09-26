"""Embeddings beside the index: incremental, metadata only, amber off by
default, a failure logged and never fatal. The platform is a stub on the
client's `embed` method; nothing here talks HTTP."""
import io
import json
import os
import unittest
from unittest import mock

from tests.test_promoter import (HAVE_WEB, PromoterBase, FORM, DEST, VOCAB)

if HAVE_WEB:
    import pyarrow.parquet as pq
    from crsw_deposit import deposit_logic, record
    from crsw_web import llm
    from promoter import __main__ as cli
    from promoter import embed as embed_mod
    from promoter.config import ConfigError, PromoterConfig

PARENT = "rs2/csac/amber/1_interim/parent"
PARENT_UUID = "22222222-2222-4222-8222-222222222222"
LLM_ENV = {"PROMOTER_LLM_BASE_URL": "https://ai.example/api/v1",
           "PROMOTER_LLM_API_KEY": "sk-test"}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class EmbedBase(PromoterBase):
    ENV = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
           "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
           "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": "",
           "PROMOTER_AUDIT_PREFIX": ""}

    def setUp(self):
        super().setUp()
        self.sent = []          # every text the stub was asked to embed

        def fake_embed(platform, texts, batch=32):
            self.sent.extend(texts)
            return [[float(len(t)), 0.5, -1.0] for t in texts]
        self.embed_patch = mock.patch.object(llm.Platform, "embed", fake_embed)
        self.embed_patch.start(); self.addCleanup(self.embed_patch.stop)

    def cli(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(self.ENV, **(env or {}))), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary", return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(argv + ["--env-file", os.devnull])
        return code, [c.args[0] for c in printed.call_args_list if c.args]

    def put_parent(self, abstract="parent words " * 10, modified="2026-01-01T00:00:00Z"):
        meta = dict(FORM, sensitivity="amber", state="1_interim", dataset="parent",
                    abstract=abstract)
        rec, _, _, _ = deposit_logic.assemble_record(
            meta, None, [record.manifest_entry("p.csv", "c" * 64, 3)],
            "alice", modified, PARENT_UUID)
        self.s3.put_object(Bucket="crsw", Key=PARENT + "/dataset.parent.json",
                           Body=deposit_logic.record_bytes(rec),
                           Metadata={"depositor": "alice"})

    def table(self):
        body = self.s3.get_object(Bucket="crsw", Key="index/embeddings.parquet")["Body"].read()
        return {r["identifier"]: r for r in pq.read_table(io.BytesIO(body)).to_pylist()}

    def summary(self, out):
        return next(json.loads(o) for o in out if '"embeddings_written"' in o)


class TestIndexEmbed(EmbedBase):
    def test_unconfigured_is_a_plain_refusal(self):
        self.stage(); self.cli(["run"])
        code, out = self.cli(["index", "--embed"])
        self.assertEqual(code, 2)
        self.assertNotIn("index/embeddings.parquet", self.keys_under("index/"))
        # Half a configuration is a configuration error.
        with self.assertRaises(ConfigError):
            PromoterConfig.from_env(dict(self.ENV, PROMOTER_LLM_API_KEY="k"))

    def test_first_build_embeds_green_only_from_metadata(self):
        self.put_parent()
        self.stage(); self.cli(["run"])
        code, out = self.cli(["index", "--embed"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        s = self.summary(out)
        self.assertEqual((s["datasets"], s["embedded"], s["kept"], s["skipped_amber"],
                          s["dropped"], s["dimension"], s["key"]),
                         (2, 1, 0, 1, 0, 3, "index/embeddings.parquet"))
        rows = self.table()
        self.assertEqual(sorted(rows), [PARENT, DEST])
        self.assertIsNone(rows[PARENT]["embedding"])
        self.assertEqual(rows[PARENT]["sensitivity"], "amber")
        self.assertEqual(rows[DEST]["model"], "arc:embedvl")
        self.assertEqual(rows[DEST]["dimension"], 3)
        self.assertEqual(len(rows[DEST]["embedding"]), 3)
        self.assertEqual(rows[DEST]["dataset_uuid"], self.jsonl_uuid(DEST))
        # What went to the platform: title, abstract, subjects. No file names.
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]
        self.assertTrue(text.startswith("promo\n"))
        self.assertIn(FORM["abstract"].strip(), text)
        self.assertIn("forced-labour", text)
        self.assertNotIn("one.csv", text)
        self.assertNotIn("r.md", text)

    def jsonl_uuid(self, identifier):
        body = self.s3.get_object(Bucket="crsw", Key="index/datasets.jsonl")["Body"].read()
        rows = [json.loads(l) for l in body.decode().splitlines()]
        return next(r["dataset_uuid"] for r in rows if r["identifier"] == identifier)

    def test_incremental_keep_replace_drop_and_model_change(self):
        self.put_parent()
        self.stage(); self.cli(["run"])
        self.cli(["index", "--embed"], env=LLM_ENV)
        self.sent.clear()
        # Nothing changed: nothing sent.
        code, out = self.cli(["index", "--embed"], env=LLM_ENV)
        s = self.summary(out)
        self.assertEqual((s["embedded"], s["kept"]), (0, 1))
        self.assertEqual(self.sent, [])
        # Amber allowed now: the parent is embedded, the green row kept.
        code, out = self.cli(["index", "--embed"], env=dict(LLM_ENV, PROMOTER_LLM_EMBED_AMBER="1"))
        s = self.summary(out)
        self.assertEqual((s["embedded"], s["kept"], s["skipped_amber"]), (1, 1, 0))
        self.assertIsNotNone(self.table()[PARENT]["embedding"])
        # Allowed no longer: the amber vector is removed again.
        self.cli(["index", "--embed"], env=LLM_ENV)
        self.assertIsNone(self.table()[PARENT]["embedding"])
        # A record modified since is re-embedded.
        self.sent.clear()
        self.put_parent(abstract="new words " * 10, modified="2026-02-02T00:00:00Z")
        code, out = self.cli(["index", "--embed"], env=dict(LLM_ENV, PROMOTER_LLM_EMBED_AMBER="1"))
        self.assertEqual(self.summary(out)["embedded"], 1)
        self.assertIn("new words", self.sent[0])
        # A model change re-embeds everything.
        self.sent.clear()
        code, out = self.cli(["index", "--embed"],
                             env=dict(LLM_ENV, PROMOTER_LLM_EMBED_AMBER="1",
                                      PROMOTER_LLM_EMBED_MODEL="other:v2"))
        s = self.summary(out)
        self.assertEqual((s["embedded"], s["kept"]), (2, 0))
        self.assertEqual({r["model"] for r in self.table().values()}, {"other:v2"})
        # A dataset gone from the store is dropped from the file.
        self.s3.delete_object(Bucket="crsw", Key=PARENT + "/dataset.parent.json")
        code, out = self.cli(["index", "--embed"], env=dict(LLM_ENV, PROMOTER_LLM_EMBED_MODEL="other:v2"))
        s = self.summary(out)
        self.assertEqual((s["datasets"], s["dropped"], s["kept"]), (1, 1, 1))
        self.assertEqual(list(self.table()), [DEST])

    def test_a_run_refreshes_embeddings_and_a_failure_is_only_logged(self):
        self.stage()
        code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        lines = [json.loads(o) for o in out]
        actions = [l["action"] for l in lines]
        self.assertEqual(actions[-3:], ["index_written", "embeddings_written", "finish"])
        self.assertEqual(self.summary(out)["embedded"], 1)
        # Platform down on the next run that changes something: logged, exit 0.
        self.stage(dataset="second")
        with mock.patch.object(llm.Platform, "embed",
                               side_effect=llm.PlatformError("platform returned HTTP 503")):
            code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0)
        failed = next(json.loads(o) for o in out if '"embeddings_failed"' in o)
        self.assertIn("503", failed["error"])
        # The previous file is untouched.
        self.assertEqual(list(self.table()), [DEST])
        # Without the platform configured a run never mentions embeddings.
        self.stage(dataset="third")
        code, out = self.cli(["run"])
        self.assertNotIn("embeddings", "".join(out))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestTextFor(unittest.TestCase):
    def test_only_the_listed_fields(self):
        row = {"dataset": "csac-clean", "abstract": "  An extract.  ", "subject": ["a", "b"],
               "source_detail": "", "files": 3, "record_key": "x/dataset.x.json",
               "depositor": "k1", "identifier": "rs2/csac/green/2_final/csac-clean"}
        self.assertEqual(embed_mod.text_for(row), "csac-clean\nAn extract.\na, b")
        self.assertEqual(embed_mod.text_for({}), "")
