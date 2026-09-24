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
        # created = when the researcher finalised, not when it was promoted.
        self.assertEqual(rec["created"], dep.finalised)
        self.assertGreaterEqual(rec["modified"], rec["created"])
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

    def test_05_destination_record_is_merged_and_written_as_06(self):
        # r8 §5 (decided): nobody converts a record by hand. A 0.5
        # destination with a string derived_from merges with a 0.6 deposit;
        # the result is 0.6 with the reference upgraded and carried over.
        dep = self.stage()
        old_uuid = "11111111-1111-4111-8111-111111111111"
        existing, _, _, _ = deposit_logic.assemble_record(
            dep.meta, None, [record.manifest_entry("old.csv", "b" * 64, 3)],
            "alice", "2026-01-01T00:00:00Z", old_uuid)
        existing["schema_version"] = "0.5"
        existing["derived_from"] = "https://github.com/jrnold/CDB90"
        self.s3.put_object(Bucket="crsw", Key=DEST + "/dataset.promo.json",
                           Body=deposit_logic.record_bytes(existing))
        self.s3.put_object(Bucket="crsw", Key=DEST + "/old.csv", Body=b"old")
        rep = check(dep, self.store, self.cfg, TERMS, CODES)
        self.assertEqual(rep.problems, [])
        self.assertTrue(any("schema 0.5" in w for w in rep.warnings), rep.warnings)
        out = promote(dep, rep, self.store, self.log, TERMS, CODES)
        self.assertTrue(out.promoted, out.error)
        raw = self.s3.get_object(Bucket="crsw", Key=DEST + "/dataset.promo.json")["Body"].read().decode()
        rec = json.loads(raw)
        self.assertEqual(rec["schema_version"], "0.6")
        self.assertEqual(rec["derived_from"],
                         [{"kind": "external", "url": "https://github.com/jrnold/CDB90"}])
        self.assertEqual(rec["dataset_uuid"], old_uuid)

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


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestRecategorise(PromoterBase):
    """r9 step C: records in place are brought up to the vocabulary's
    changes and a steward's change file; stale staged records are mapped
    at promotion time."""

    NOW = "2026-10-05T10:00:00Z"

    def evolved(self):
        from crsw_deposit import authority
        d = authority.apply_change(VOCAB, {"kind": "merge", "from": ["debt-bondage"],
                                           "to": ["forced-labour"],
                                           "date": "2026-10-01", "by": "k1"})
        d = authority.apply_change(d, {"kind": "add", "to": ["survey-household"],
                                       "facet": "methods", "label": "Household",
                                       "date": "2026-10-02", "by": "k1"})
        d = authority.apply_change(d, {"kind": "add", "to": ["survey-online"],
                                       "facet": "methods", "label": "Online",
                                       "date": "2026-10-02", "by": "k1"})
        d = authority.apply_change(d, {"kind": "split", "from": ["survey"],
                                       "to": ["survey-household", "survey-online"],
                                       "date": "2026-10-03", "by": "k1"})
        return d

    def place(self, dataset, subject, domain="quant", uuid=None):
        """A dataset record in place, as promotion would have written it."""
        from tests.test_schema import worked_example
        rec = worked_example()
        rec.update(identifier="rs2/csac/green/2_final/%s" % dataset, dataset=dataset,
                   subject=list(subject), domain=domain,
                   dataset_uuid=uuid or record.mint_uuid())
        rec.pop("category_history", None)
        key = "rs2/csac/green/2_final/%s/dataset.%s.json" % (dataset, dataset)
        data = deposit_logic.record_bytes(rec)
        self.s3.put_object(Bucket="crsw", Key=key, Body=data,
                           Metadata=labels_mod.object_labels(
                               rec["dataset_uuid"], hashlib.sha256(data).hexdigest(),
                               "green", "k1078591"))
        return key, data, rec

    def read(self, key):
        return json.loads(self.s3.get_object(Bucket="crsw", Key=key)["Body"].read())

    def run_cli(self, command, vocab_doc, *extra):
        env = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
               "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
               "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary",
                        return_value=(vocab_doc, "remote")), \
             mock.patch("builtins.print") as printed:
            code = cli.main([command, "--env-file", os.devnull] + list(extra))
        lines = [json.loads(c.args[0]) for c in printed.call_args_list
                 if c.args and isinstance(c.args[0], str) and c.args[0].startswith("{")]
        return code, lines

    def test_list_datasets_walks_strand_prefixes_only(self):
        from promoter.scan import list_datasets
        self.place("b", ["forced-labour"])
        self.place("a", ["forced-labour"])
        self.s3.put_object(Bucket="crsw", Key="rs2/csac/green/2_final/a/notes.json", Body=b"{}")
        self.s3.put_object(Bucket="crsw", Key="staging/_test/k/x/rs2/csac/green/2_final/c/dataset.c.json", Body=b"{}")
        self.s3.put_object(Bucket="crsw", Key="rs2/csac/green/2_final/d/dataset.e.json", Body=b"{}")
        refs = list(list_datasets(self.s3, "crsw"))
        self.assertEqual([r.prefix for r in refs],
                         ["rs2/csac/green/2_final/a", "rs2/csac/green/2_final/b"])

    def test_vocabulary_merge_rewrites_affected_records_only(self):
        k1, d1, _ = self.place("one", ["debt-bondage", "armed-conflict"])
        k2, d2, _ = self.place("two", ["forced-labour", "debt-bondage"])
        k3, d3, _ = self.place("three", ["armed-conflict"])
        code, lines = self.run_cli("recategorise", self.evolved(), "--by", "k1078591")
        self.assertEqual(code, 0, lines)
        finish = lines[-1]
        self.assertEqual((finish["seen"], finish["rewritten"], finish["refused_or_failed"]),
                         (3, 2, 0))
        one = self.read(k1)
        self.assertEqual(one["subject"], ["forced-labour", "armed-conflict"])
        self.assertEqual(one["vocabulary_version"], "2026-10-03")
        self.assertEqual(one["category_history"][-1]["by"], "vocabulary")
        self.assertEqual(one["category_history"][-1]["kind"], "replaced")
        self.assertEqual(self.read(k2)["subject"], ["forced-labour"])
        # the third record has the same bytes: never rewritten
        self.assertEqual(self.s3.get_object(Bucket="crsw", Key=k3)["Body"].read(), d3)
        # rewritten records keep their uuid and created, change modified
        self.assertEqual(one["dataset_uuid"], json.loads(d1)["dataset_uuid"])
        self.assertEqual(one["created"], json.loads(d1)["created"])
        self.assertNotEqual(one["modified"], json.loads(d1)["modified"])
        # record object labels are fresh
        head = self.s3.head_object(Bucket="crsw", Key=k1)["Metadata"]
        self.assertEqual(head["checksum-sha256"],
                         hashlib.sha256(deposit_logic.record_bytes(one)).hexdigest())
        # second run: nothing to do
        code, lines = self.run_cli("recategorise", self.evolved())
        self.assertEqual(code, 0)
        self.assertEqual((lines[-1]["seen"], lines[-1]["rewritten"]), (3, 0))

    def test_split_gives_all_successors_with_review_entry(self):
        k, _, _ = self.place("s", ["survey", "armed-conflict"])
        code, lines = self.run_cli("recategorise", self.evolved())
        self.assertEqual(code, 0)
        rec = self.read(k)
        self.assertEqual(rec["subject"], ["survey-household", "survey-online", "armed-conflict"])
        entry = rec["category_history"][-1]
        self.assertEqual((entry["kind"], entry["from"], entry["to"]),
                         ("split_review", ["survey"], ["survey-household", "survey-online"]))

    def test_change_file_add_remove_domain(self):
        k, _, rec = self.place("c", ["forced-labour", "survey-household"], domain="quant")
        self.place("other", ["forced-labour"])
        changes = [{"dataset": rec["dataset_uuid"], "add": ["prevalence"],
                    "remove": ["survey-household"], "set_domain": "geo",
                    "reason": "steward review"}]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "changes.json"
            path.write_text(json.dumps(changes), encoding="utf-8")
            code, lines = self.run_cli("recategorise", self.evolved(),
                                       "--changes", str(path), "--by", "k1078591")
        self.assertEqual(code, 0, lines)
        out = self.read(k)
        self.assertEqual(out["subject"], ["forced-labour", "prevalence"])
        self.assertEqual(out["domain"], "geo")
        kinds = [e["kind"] for e in out["category_history"]]
        self.assertEqual(kinds, ["removed", "added", "domain_changed"])
        self.assertTrue(all(e["by"] == "k1078591" and e["reason"] == "steward review"
                            for e in out["category_history"]))
        self.assertEqual(lines[-1]["rewritten"], 1)
        planned = [l for l in lines if l["action"] == "recategorise_planned"]
        self.assertEqual(planned[0]["subject_before"], ["forced-labour", "survey-household"])

    def test_change_file_refusals_write_nothing(self):
        k, data, rec = self.place("r", ["forced-labour"])
        cases = [
            ([{"dataset": rec["identifier"], "add": ["made-up"]}], "not in the vocabulary"),
            ([{"dataset": "rs2/csac/green/2_final/nope", "add": ["prevalence"]}],
             "no dataset with that identifier"),
            ([{"dataset": rec["identifier"], "remove": ["forced-labour"]}],
             "at least one subject"),
        ]
        for changes, fragment in cases:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "changes.json"
                path.write_text(json.dumps(changes), encoding="utf-8")
                code, lines = self.run_cli("recategorise", self.evolved(),
                                           "--changes", str(path))
            self.assertEqual(code, 1, fragment)
            refused = [l for l in lines if l["action"] == "recategorise_refused"]
            self.assertTrue(any(fragment in json.dumps(l) for l in refused), (fragment, lines))
            self.assertEqual(self.s3.get_object(Bucket="crsw", Key=k)["Body"].read(), data)

    def test_bad_change_file_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "changes.json"
            path.write_text('[{"dataset": "x"}]', encoding="utf-8")
            code, _ = self.run_cli("recategorise", self.evolved(), "--changes", str(path))
        self.assertEqual(code, 2)

    def test_dry_run_plans_and_writes_nothing(self):
        k, data, _ = self.place("dry", ["debt-bondage"])
        code, lines = self.run_cli("recategorise", self.evolved(), "--dry-run")
        self.assertEqual(code, 0)
        planned = [l for l in lines if l["action"] == "recategorise_planned"]
        self.assertEqual(planned[0]["subject_after"], ["forced-labour"])
        self.assertEqual(lines[-1]["would_rewrite"], 1)
        self.assertEqual(lines[-1]["rewritten"], 0)
        self.assertEqual(self.s3.get_object(Bucket="crsw", Key=k)["Body"].read(), data)

    def test_only_one_dataset(self):
        k1, _, _ = self.place("p", ["debt-bondage"])
        k2, d2, _ = self.place("q", ["debt-bondage"])
        code, lines = self.run_cli("recategorise", self.evolved(),
                                   "--dataset", "rs2/csac/green/2_final/p")
        self.assertEqual(code, 0)
        self.assertEqual(self.read(k1)["subject"], ["forced-labour"])
        self.assertEqual(self.s3.get_object(Bucket="crsw", Key=k2)["Body"].read(), d2)

    def test_stale_staged_record_is_mapped_at_promotion(self):
        dep = self.stage(subject=["debt-bondage", "armed-conflict"])
        code, lines = self.run_cli("run", self.evolved())
        self.assertEqual(code, 0, lines)
        self.assertTrue(any(l["action"] == "stale_terms_mapped" for l in lines))
        checked = [l for l in lines if l["action"] == "checked"][0]
        self.assertTrue(any("stale subject terms mapped" in w for w in checked["warnings"]))
        rec = self.read(DEST + "/dataset.promo.json")
        self.assertEqual(rec["subject"], ["forced-labour", "armed-conflict"])
        self.assertEqual(rec["category_history"][0]["by"], "vocabulary")
        self.assertEqual(rec["vocabulary_version"], "2026-10-03")
        errors, _ = record.validate_record(rec, vocab.all_terms(self.evolved()))
        self.assertEqual(errors, [])

    def test_staged_record_needing_a_split_decision_is_refused(self):
        dep = self.stage(subject=["survey"])
        code, lines = self.run_cli("run", self.evolved())
        self.assertEqual(code, 1)
        checked = [l for l in lines if l["action"] == "checked"][0]
        self.assertTrue(any("a person must choose" in p for p in checked["problems"]),
                        checked)
        self.assertEqual(self.keys_under("rs2/"), [])

    def test_current_staged_record_is_untouched_by_the_mapping(self):
        dep = self.stage()
        code, lines = self.run_cli("run", self.evolved())
        self.assertEqual(code, 0)
        self.assertFalse(any(l["action"] == "stale_terms_mapped" for l in lines))
        rec = self.read(DEST + "/dataset.promo.json")
        self.assertNotIn("category_history", rec)


class TestLogStdout(unittest.TestCase):
    def test_stdout_path_prints_each_line_once(self):
        import io
        from contextlib import redirect_stdout
        from promoter.log import Log
        out = io.StringIO()
        with redirect_stdout(out):
            Log("/dev/stdout").write("start")
        self.assertEqual(out.getvalue().count('"action": "start"'), 1)
