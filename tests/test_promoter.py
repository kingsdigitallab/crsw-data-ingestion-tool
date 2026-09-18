import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import boto3
    from fastapi.testclient import TestClient
    from moto import mock_aws
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    from crsw_web.deposits import DepositStore
    from promoter import __main__ as cli
    from promoter.checks import check
    from promoter.config import ConfigError, PromoterConfig
    from promoter.log import Log
    from promoter.promote import promote
    from promoter.scan import list_deposits
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import deposit_logic, labels as labels_mod, record, vocab

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
TERMS, CODES = vocab.all_terms(VOCAB), vocab.domain_codes(VOCAB)
SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t",
                s3_secret_key="t", s3_bucket="crsw", staging_prefix="staging/_test")
FORM = {
    "strand": "rs2", "sensitivity": "green", "state": "0_raw",
    "project": "csac", "dataset": "promo", "domain": "quant",
    "version": "1-0", "coverage_start": "2020", "coverage_end": "2021",
    "subject": ["forced-labour"], "abstract": "words " * 60,
}
CFG = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
           s3_bucket="crsw", staging_prefix="staging/_test", log_path="")
DEST = "rs2/csac/green/0_raw/promo"


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class PromoterBase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.mock = mock_aws(); self.mock.start(); self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")
        self.web = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3,
                                         vocab_dict=VOCAB))
        self.store = DepositStore(self.s3, "crsw", "staging/_test")
        self.cfg = PromoterConfig(**CFG)
        self.log = Log(None, echo=False)

    def stage(self, files=(("one.csv", b"a,b\n1,2\n"), ("sub/r.md", b"hi\n")),
              finalise=True, **overrides):
        d = self.web.post("/deposits", json=dict(FORM, **overrides)).json()
        for member, data in files:
            r = self.web.put("/deposits/%s/files/%s" % (d["id"], member), content=data)
            assert r.status_code == 200, r.text
        if finalise:
            r = self.web.post("/deposits/%s/finalise" % d["id"])
            assert r.status_code == 200, r.text
        return self.store.load("k1078591", d["id"])

    def keys_under(self, prefix):
        r = self.s3.list_objects_v2(Bucket="crsw", Prefix=prefix)
        return sorted(o["Key"] for o in r.get("Contents", []))


class TestChecks(PromoterBase):
    def test_clean_deposit_passes(self):
        dep = self.stage()
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertEqual(rep.problems, [])
        self.assertEqual(rep.warnings, [])
        self.assertEqual(rep.total_bytes, 11)

    def test_open_deposit_is_not_promotable(self):
        dep = self.stage(finalise=False)
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertIn("not complete", rep.problems[0])

    def test_missing_object(self):
        dep = self.stage()
        self.s3.delete_object(Bucket="crsw", Key=self.store.staged_key(
            dep.user, dep.id, DEST + "/one.csv"))
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertTrue(any("no object at" in p for p in rep.problems), rep.problems)

    def test_tampered_bytes_fail_checksum(self):
        dep = self.stage()
        key = self.store.staged_key(dep.user, dep.id, DEST + "/one.csv")
        head = self.s3.head_object(Bucket="crsw", Key=key)
        self.s3.put_object(Bucket="crsw", Key=key, Body=b"a,b\n9,9\n",
                           Metadata=head["Metadata"])   # same size, same labels
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertTrue(any("hash to" in p for p in rep.problems), rep.problems)

    def test_wrong_label(self):
        dep = self.stage()
        key = self.store.staged_key(dep.user, dep.id, DEST + "/one.csv")
        self.s3.copy_object(Bucket="crsw", Key=key, CopySource={"Bucket": "crsw", "Key": key},
                            Metadata={"depositor": "mallory"}, MetadataDirective="REPLACE")
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertTrue(any("label depositor" in p for p in rep.problems), rep.problems)

    def test_size_limits(self):
        dep = self.stage()
        cfg = PromoterConfig(**dict(CFG, max_object_bytes=5))
        rep = check(dep, self.store, cfg, TERMS, CODES)
        self.assertTrue(any("object limit" in p for p in rep.problems))
        cfg = PromoterConfig(**dict(CFG, max_deposit_bytes=10))
        rep = check(dep, self.store, cfg, TERMS, CODES)
        self.assertTrue(any("over the 10-byte limit" in p for p in rep.problems))

    def test_authorisation(self):
        dep = self.stage()
        cfg = PromoterConfig(**dict(CFG, authorised={"k1078591": ["rs1"]}))
        rep = check(dep, self.store, cfg, TERMS, CODES)
        self.assertTrue(any("not authorised" in p for p in rep.problems))
        cfg = PromoterConfig(**dict(CFG, authorised={"*": ["rs2"]}))
        self.assertEqual(check(dep, self.store, cfg, TERMS, CODES).problems, [])

    def test_existing_destination_record_is_a_warning(self):
        dep = self.stage()
        existing, _, _, _ = deposit_logic.assemble_record(
            dep.meta, None, [record.manifest_entry("old.csv", "b" * 64, 3)],
            "alice", "2026-01-01T00:00:00Z", "11111111-1111-4111-8111-111111111111")
        self.s3.put_object(Bucket="crsw", Key=DEST + "/dataset.promo.json",
                           Body=deposit_logic.record_bytes(existing))
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertEqual(rep.problems, [])
        self.assertIsNotNone(rep.existing_record)
        self.assertIn("2 member(s) added", rep.warnings[0])

    def test_hand_edited_destination_record_is_a_problem(self):
        dep = self.stage()
        bad = {"schema_version": "0.5", "identifier": "rs2/other/green/0_raw/x",
               "dataset": "x", "files": [{"path": "a", "checksum_sha256": "a" * 64, "bytes": 1}]}
        self.s3.put_object(Bucket="crsw", Key=DEST + "/dataset.promo.json",
                           Body=json.dumps(bad).encode())
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertTrue(any("moved or hand-edited" in p for p in rep.problems))


class TestPromote(PromoterBase):
    def test_promotes_members_then_record_then_clears_staging(self):
        dep = self.stage()
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        out = promote(dep, rep, self.store, self.log, TERMS, CODES)
        self.assertTrue(out.promoted, out.error)
        self.assertEqual(self.keys_under(DEST + "/"), [
            DEST + "/dataset.promo.json", DEST + "/one.csv", DEST + "/sub/r.md"])
        head = self.s3.head_object(Bucket="crsw", Key=DEST + "/one.csv")
        self.assertEqual(head["Metadata"], labels_mod.object_labels(
            dep.dataset_uuid, hashlib.sha256(b"a,b\n1,2\n").hexdigest(), "green", "k1078591"))
        rec = record.parse_record(self.s3.get_object(
            Bucket="crsw", Key=DEST + "/dataset.promo.json")["Body"].read().decode())
        self.assertEqual(rec["dataset_uuid"], dep.dataset_uuid)
        self.assertEqual([e["path"] for e in rec["files"]], ["one.csv", "sub/r.md"])
        # Staging is empty (delete markers), including the control object.
        self.assertEqual(self.keys_under("staging/"), [])
        actions = [l["action"] for l in self.log.lines]
        self.assertEqual(actions, ["copied", "copied", "record_written", "promoted"])

    def test_merge_with_existing_keeps_uuid_and_created(self):
        dep = self.stage()
        old_uuid = "11111111-1111-4111-8111-111111111111"
        existing, _, _, _ = deposit_logic.assemble_record(
            dict(dep.meta, coverage_start="2018"), None,
            [record.manifest_entry("old.csv", "b" * 64, 3)],
            "alice", "2026-01-01T00:00:00Z", old_uuid)
        self.s3.put_object(Bucket="crsw", Key=DEST + "/dataset.promo.json",
                           Body=deposit_logic.record_bytes(existing))
        self.s3.put_object(Bucket="crsw", Key=DEST + "/old.csv", Body=b"old")
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        out = promote(dep, rep, self.store, self.log, TERMS, CODES)
        self.assertTrue(out.promoted, out.error)
        self.assertEqual(out.dataset_uuid, old_uuid)
        rec = record.parse_record(self.s3.get_object(
            Bucket="crsw", Key=DEST + "/dataset.promo.json")["Body"].read().decode())
        self.assertEqual(rec["dataset_uuid"], old_uuid)
        self.assertEqual(rec["created"], "2026-01-01T00:00:00Z")
        self.assertEqual(rec["depositors"], ["alice", "k1078591"])
        self.assertEqual(rec["temporal"]["start"], "2018")
        self.assertEqual([e["path"] for e in rec["files"]], ["old.csv", "one.csv", "sub/r.md"])
        # Copied objects carry the KEPT uuid.
        head = self.s3.head_object(Bucket="crsw", Key=DEST + "/one.csv")
        self.assertEqual(head["Metadata"]["dataset-uuid"], old_uuid)

    def test_keep_staging(self):
        dep = self.stage()
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        out = promote(dep, rep, self.store, self.log, TERMS, CODES, keep_staging=True)
        self.assertTrue(out.promoted)
        self.assertEqual(len(self.keys_under("staging/")), 4)

    def test_multipart_copy_path(self):
        data = os.urandom(6 * 1024 * 1024)
        dep = self.stage(files=(("big.bin", data),))
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        out = promote(dep, rep, self.store, self.log, TERMS, CODES,
                      single_copy_max=1024, copy_part=5 * 1024 * 1024)
        self.assertTrue(out.promoted, out.error)
        obj = self.s3.get_object(Bucket="crsw", Key=DEST + "/big.bin")
        self.assertEqual(obj["Body"].read(), data)
        self.assertEqual(obj["Metadata"]["checksum-sha256"], hashlib.sha256(data).hexdigest())

    def test_refuses_without_ok_report(self):
        dep = self.stage()
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        rep.problems.append("x")
        from promoter.promote import PromotionError
        with self.assertRaises(PromotionError):
            promote(dep, rep, self.store, self.log, TERMS, CODES)


class TestRun(PromoterBase):
    def run_cli(self, *extra):
        env = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
               "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
               "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary", return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(["run", "--env-file", os.devnull] + list(extra))
        lines = [json.loads(c.args[0]) for c in printed.call_args_list]
        return code, lines

    def test_dry_run_reports_and_moves_nothing(self):
        good = self.stage()
        bad = self.stage(dataset="promo-bad")
        self.s3.delete_object(Bucket="crsw", Key=self.store.staged_key(
            bad.user, bad.id, "rs2/csac/green/0_raw/promo-bad/one.csv"))
        code, lines = self.run_cli("--dry-run")
        self.assertEqual(code, 1)
        checked = {l["deposit"]: l for l in lines if l["action"] == "checked"}
        self.assertTrue(checked[good.id]["ok"])
        self.assertFalse(checked[bad.id]["ok"])
        self.assertEqual([l["action"] for l in lines if l["action"] == "would_promote"],
                         ["would_promote"])
        self.assertEqual(self.keys_under("rs2/"), [])
        self.assertEqual(len(self.keys_under("staging/")), 7)  # 4 + 3 (one deleted)

    def test_run_promotes_good_refuses_bad_then_noop(self):
        good = self.stage()
        bad = self.stage(dataset="promo-bad")
        self.s3.delete_object(Bucket="crsw", Key=self.store.staged_key(
            bad.user, bad.id, "rs2/csac/green/0_raw/promo-bad/one.csv"))
        code, lines = self.run_cli()
        self.assertEqual(code, 1)
        finish = lines[-1]
        self.assertEqual((finish["promoted"], finish["refused_or_failed"]), (1, 1))
        self.assertEqual(len(self.keys_under(DEST + "/")), 3)
        self.assertEqual(self.keys_under("staging/_test/k1078591/%s/" % good.id), [])
        self.assertEqual(len(self.keys_under("staging/_test/k1078591/%s/" % bad.id)), 3)
        # Second run: the bad one is refused again, nothing else to do.
        code, lines = self.run_cli()
        self.assertEqual(lines[-1]["promoted"], 0)
        self.assertEqual(lines[-1]["seen"], 1)

    def test_filter_by_deposit(self):
        a = self.stage()
        b = self.stage(dataset="promo-b")
        code, lines = self.run_cli("--deposit", b.id)
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["seen"], 1)
        self.assertEqual(len(self.keys_under("staging/_test/k1078591/%s/" % a.id)), 4)

    def test_config_error_exit_2(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.print"):
            self.assertEqual(cli.main(["run", "--env-file", os.devnull]), 2)


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestConfig(unittest.TestCase):
    def test_authorised_parsing(self):
        base = {"PROMOTER_S3_ENDPOINT": "e", "PROMOTER_S3_ACCESS_KEY": "a",
                "PROMOTER_S3_SECRET_KEY": "s", "PROMOTER_S3_BUCKET": "b",
                "PROMOTER_STAGING_PREFIX": "staging/x/"}
        cfg = PromoterConfig.from_env(base)
        self.assertEqual(cfg.authorised, "*")
        self.assertEqual(cfg.staging_prefix, "staging/x")
        cfg = PromoterConfig.from_env(dict(base, PROMOTER_AUTHORISED='{"u": ["rs1"]}'))
        self.assertTrue(cfg.user_may_deposit_to("u", "rs1"))
        self.assertFalse(cfg.user_may_deposit_to("u", "rs2"))
        self.assertFalse(cfg.user_may_deposit_to("v", "rs1"))
        with self.assertRaises(ConfigError):
            PromoterConfig.from_env(dict(base, PROMOTER_AUTHORISED="nope"))
        with self.assertRaises(ConfigError):
            PromoterConfig.from_env(dict(base, PROMOTER_MAX_OBJECT_BYTES="big"))
