import hashlib
import io
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
    from promoter import index as index_mod
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


def finish(lines):
    return [l for l in lines if l["action"] == "finish"][-1]


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


class TestResolveReferences(PromoterBase):
    """r8 §4: the promoter fills in a parent's uuid and version, in
    staging first, then promotes; a missing parent is a warning."""

    PARENT = "rs2/csac/amber/1_interim/parent"
    PARENT_UUID = "22222222-2222-4222-8222-222222222222"

    def seed_parent(self):
        meta = dict(FORM, sensitivity="amber", state="1_interim",
                    dataset="parent", version="2-1")
        rec, _, _, _ = deposit_logic.assemble_record(
            meta, None, [record.manifest_entry("p.csv", "c" * 64, 3)],
            "alice", "2026-01-01T00:00:00Z", self.PARENT_UUID)
        self.s3.put_object(Bucket="crsw", Key=self.PARENT + "/dataset.parent.json",
                           Body=deposit_logic.record_bytes(rec))

    def run_cli(self, *extra):
        env = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
               "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
               "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": "",
               "PROMOTER_AUDIT_PREFIX": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary", return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(["run", "--env-file", os.devnull] + list(extra))
        return code, [json.loads(c.args[0]) for c in printed.call_args_list]

    def test_parent_uuid_and_version_are_filled_in_staging_then_promoted(self):
        self.seed_parent()
        dep = self.stage(derived_from=self.PARENT + "\nhttps://x.org/a")
        code, lines = self.run_cli("--keep-staging")
        self.assertEqual(code, 0, lines)
        actions = [l["action"] for l in lines]
        self.assertIn("resolved_reference", actions)
        self.assertLess(actions.index("resolved_reference"), actions.index("record_rewritten"))
        self.assertLess(actions.index("record_rewritten"), actions.index("copied"))
        res = next(l for l in lines if l["action"] == "resolved_reference")
        self.assertEqual((res["identifier"], res["dataset_uuid"], res["version"]),
                         (self.PARENT, self.PARENT_UUID, "2-1"))
        rew = next(l for l in lines if l["action"] == "record_rewritten")
        self.assertEqual((rew["where"], rew["reason"]), ("staging", "resolved_reference"))
        # The destination record carries the resolved reference, and the
        # external one is untouched.
        raw = self.s3.get_object(Bucket="crsw", Key=DEST + "/dataset.promo.json")["Body"].read()
        rec = json.loads(raw)
        self.assertEqual(rec["derived_from"], [
            {"kind": "dataset", "identifier": self.PARENT,
             "dataset_uuid": self.PARENT_UUID, "version": "2-1"},
            {"kind": "external", "url": "https://x.org/a"}])
        # The staged record was rewritten to say the same before the move
        # (kept here by --keep-staging; on a real run it becomes a
        # noncurrent version behind the delete marker).
        staged = json.loads(self.s3.get_object(Bucket="crsw", Key=dep.record_key)["Body"].read())
        self.assertEqual(staged["derived_from"], rec["derived_from"])
        head = self.s3.head_object(Bucket="crsw", Key=dep.record_key)
        self.assertEqual(head["Metadata"]["dataset-uuid"], dep.dataset_uuid)
        self.assertEqual(head["Metadata"]["depositor"], "k1078591")

    def test_missing_parent_is_a_warning_and_the_deposit_still_promotes(self):
        dep = self.stage(derived_from=self.PARENT)
        code, lines = self.run_cli("--dry-run")
        self.assertEqual(code, 0, lines)
        checked = next(l for l in lines if l["action"] == "checked")
        self.assertTrue(checked["ok"])
        self.assertTrue(any("no dataset record" in w for w in checked["warnings"]), checked)
        self.assertEqual([l["identifier"] for l in lines if l["action"] == "reference_unresolved"],
                         [self.PARENT])
        self.assertNotIn("resolved_reference", [l["action"] for l in lines])
        code, lines = self.run_cli()
        self.assertEqual(code, 0)
        self.assertNotIn("record_rewritten", [l["action"] for l in lines])
        rec = json.loads(self.s3.get_object(
            Bucket="crsw", Key=DEST + "/dataset.promo.json")["Body"].read())
        self.assertEqual(rec["derived_from"],
                         [{"kind": "dataset", "identifier": self.PARENT}])

    def test_dry_run_reports_the_resolution_and_writes_nothing(self):
        self.seed_parent()
        dep = self.stage(derived_from=self.PARENT)
        before = self.s3.get_object(Bucket="crsw", Key=dep.record_key)["Body"].read()
        code, lines = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        res = [l for l in lines if l["action"] == "resolved_reference"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["dry_run"])
        self.assertEqual(self.s3.get_object(Bucket="crsw", Key=dep.record_key)["Body"].read(),
                         before)
        self.assertEqual(self.keys_under("rs2/csac/green/"), [])

    def test_already_resolved_reference_is_left_alone(self):
        self.seed_parent()
        from promoter.resolve import lookup
        refs = [{"kind": "dataset", "identifier": self.PARENT,
                 "dataset_uuid": "33333333-3333-4333-8333-333333333333", "version": "9-9"}]
        out, resolved, unresolved = lookup(self.s3, "crsw", refs)
        self.assertEqual((out, resolved, unresolved), (refs, [], []))
        out, resolved, unresolved = lookup(self.s3, "crsw", [
            {"kind": "dataset", "identifier": self.PARENT, "version": "9-9"}])
        self.assertEqual(out[0]["dataset_uuid"], self.PARENT_UUID)
        self.assertEqual(out[0]["version"], "9-9")     # kept: the depositor said which


class TestIndex(PromoterBase):
    """The index: rewritten after a real run that changed something,
    rebuilt on demand, readable as JSON lines and Parquet."""

    ENV = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
           "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
           "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": "",
           "PROMOTER_AUDIT_PREFIX": ""}

    def cli(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(self.ENV, **(env or {}))), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary", return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(argv + ["--env-file", os.devnull])
        return code, [c.args[0] for c in printed.call_args_list if c.args]

    def jsonl_rows(self):
        body = self.s3.get_object(Bucket="crsw", Key="index/datasets.jsonl")["Body"].read()
        return [json.loads(l) for l in body.decode().splitlines()]

    def test_run_that_promotes_writes_both_index_objects(self):
        parent_uuid = "22222222-2222-4222-8222-222222222222"
        parent = "rs2/csac/amber/1_interim/parent"
        meta = dict(FORM, sensitivity="amber", state="1_interim", dataset="parent")
        rec, _, _, _ = deposit_logic.assemble_record(
            meta, None, [record.manifest_entry("p.csv", "c" * 64, 3)],
            "alice", "2026-01-01T00:00:00Z", parent_uuid)
        self.s3.put_object(Bucket="crsw", Key=parent + "/dataset.parent.json",
                           Body=deposit_logic.record_bytes(rec),
                           Metadata={"depositor": "alice"})
        self.stage(derived_from=parent, provenance_activity="harmonise",
                   provenance_tool="cdisaw-parquet")
        code, out = self.cli(["run"])
        self.assertEqual(code, 0, out)
        lines = [json.loads(o) for o in out]
        written = next(l for l in lines if l["action"] == "index_written")
        self.assertEqual((written["datasets"], written["jsonl"], written["parquet"]),
                         (2, "index/datasets.jsonl", "index/datasets.parquet"))
        # Part of the run, so inside the audit object: before finish.
        actions = [l["action"] for l in lines]
        self.assertEqual(actions[-2:], ["index_written", "finish"])
        rows = self.jsonl_rows()
        self.assertEqual([r["identifier"] for r in rows], [parent, DEST])
        promo = rows[1]
        self.assertEqual(promo["dataset"], "promo")
        self.assertEqual(promo["depositor"], "k1078591")
        self.assertEqual(promo["depositors"], ["k1078591"])
        self.assertEqual(promo["subject"], FORM["subject"])
        self.assertEqual((promo["temporal_start"], promo["temporal_end"]), ("2020", "2021"))
        self.assertEqual((promo["files"], promo["bytes"]), (2, 11))
        self.assertEqual(promo["derived_from_identifiers"], [parent])
        self.assertEqual(json.loads(promo["derived_from"])[0]["dataset_uuid"], parent_uuid)
        self.assertEqual(promo["provenance_tools"], ["cdisaw-parquet"])
        self.assertEqual(promo["origin"], "cdisaw-parquet")
        self.assertEqual(rows[0]["origin"], "deposit")
        self.assertEqual(rows[0]["depositor"], "alice")
        self.assertTrue(promo["indexed_at"].endswith("Z"))
        self.assertEqual(promo["record_key"], DEST + "/dataset.promo.json")
        # The Parquet copy says the same.
        import pyarrow.parquet as pq
        body = self.s3.get_object(Bucket="crsw", Key="index/datasets.parquet")["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        self.assertEqual(table.num_rows, 2)
        self.assertEqual(list(table.column_names), list(index_mod.COLUMNS))
        self.assertEqual(table.column("subject").to_pylist()[1], FORM["subject"])
        self.assertEqual(table.column("bytes").to_pylist(), [3, 11])

    def test_nothing_changed_means_nothing_written(self):
        self.stage()
        code, out = self.cli(["run", "--dry-run"])
        self.assertEqual(self.keys_under("index/"), [])
        self.assertNotIn("index_written", "".join(out))
        self.cli(["run"])
        first = self.s3.head_object(Bucket="crsw", Key="index/datasets.jsonl")["ETag"]
        code, out = self.cli(["run"])              # nothing to do
        self.assertEqual(code, 0)
        self.assertNotIn("index_written", "".join(out))
        self.assertEqual(self.s3.head_object(Bucket="crsw", Key="index/datasets.jsonl")["ETag"], first)

    def test_blank_prefix_disables_and_index_command_rebuilds(self):
        self.stage()
        code, out = self.cli(["run"], env={"PROMOTER_INDEX_PREFIX": ""})
        self.assertEqual(code, 0)
        self.assertEqual(self.keys_under("index/"), [])
        code, out = self.cli(["index"], env={"PROMOTER_INDEX_PREFIX": ""})
        self.assertEqual(code, 2)
        code, out = self.cli(["index"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out[-1])["datasets"], 1)
        self.assertEqual(self.keys_under("index/"),
                         ["index/datasets.jsonl", "index/datasets.parquet"])
        self.assertEqual(self.jsonl_rows()[0]["identifier"], DEST)

    def test_write_failure_is_logged_and_leaves_the_exit_code(self):
        self.stage()
        with mock.patch("promoter.index.write", side_effect=OSError("bucket gone")):
            code, out = self.cli(["run"])
        self.assertEqual(code, 0)
        self.assertIn("index_write_failed", "".join(out))
        self.assertEqual(self.keys_under("index/"), [])

    def test_old_record_and_unreadable_record_still_get_a_row(self):
        old = {"schema_version": "0.5", "dataset_uuid": "11111111-1111-4111-8111-111111111111",
               "identifier": "rs1/p/green/0_raw/old", "strand": "rs1", "project": "p",
               "dataset": "old", "state": "0_raw", "sensitivity": "green", "domain": "quant",
               "version": "1-0", "abstract": "x", "subject": ["forced-labour"],
               "temporal": {"start": "1990", "end": "1991"}, "created": "2026-01-01T00:00:00Z",
               "modified": "2026-01-01T00:00:00Z", "files": [],
               "derived_from": "rs1/p/green/0_raw/older"}
        self.s3.put_object(Bucket="crsw", Key="rs1/p/green/0_raw/old/dataset.old.json",
                           Body=json.dumps(old).encode())
        self.s3.put_object(Bucket="crsw", Key="rs1/p/green/0_raw/bad/dataset.bad.json",
                           Body=b"{not json")
        rows = index_mod.rows(self.s3, "crsw", now="2026-09-25T00:00:00Z")
        self.assertEqual([r["identifier"] for r in rows],
                         ["rs1/p/green/0_raw/bad", "rs1/p/green/0_raw/old"])
        self.assertEqual(rows[0]["dataset_uuid"], None)
        self.assertEqual(rows[1]["derived_from_identifiers"], ["rs1/p/green/0_raw/older"])
        self.assertEqual(rows[1]["schema_version"], "0.6")   # upgraded on read
        self.assertIsNotNone(index_mod.parquet_bytes(rows))


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
        fin = finish(lines)
        self.assertEqual((fin["promoted"], fin["refused_or_failed"]), (1, 1))
        self.assertEqual(len(self.keys_under(DEST + "/")), 3)
        self.assertEqual(self.keys_under("staging/_test/k1078591/%s/" % good.id), [])
        self.assertEqual(len(self.keys_under("staging/_test/k1078591/%s/" % bad.id)), 3)
        # Second run: the bad one is refused again, nothing else to do.
        code, lines = self.run_cli()
        self.assertEqual(finish(lines)["promoted"], 0)
        self.assertEqual(finish(lines)["seen"], 1)

    def test_filter_by_deposit(self):
        a = self.stage()
        b = self.stage(dataset="promo-b")
        code, lines = self.run_cli("--deposit", b.id)
        self.assertEqual(code, 0)
        self.assertEqual(finish(lines)["seen"], 1)
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
        fin = finish(lines)
        self.assertEqual((fin["seen"], fin["rewritten"], fin["refused_or_failed"]),
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
        self.assertEqual((finish(lines)["seen"], finish(lines)["rewritten"]), (3, 0))

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
        self.assertEqual(finish(lines)["rewritten"], 1)
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
        self.assertEqual(finish(lines)["would_rewrite"], 1)
        self.assertEqual(finish(lines)["rewritten"], 0)
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


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestAuditTrail(PromoterBase):
    """Each run leaves one object in the bucket; `audit` and `datasets`
    read the bucket back."""

    ENV = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
           "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
           "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": ""}

    def cli(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(self.ENV, **(env or {}))), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary",
                        return_value=(VOCAB, "remote")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(argv + ["--env-file", os.devnull])
        out = [c.args[0] for c in printed.call_args_list if c.args]
        return code, out

    def audit_keys(self):
        return self.keys_under("audit/")

    def test_each_run_writes_one_object_named_by_its_run_id(self):
        self.stage()
        code, out = self.cli(["run"])
        self.assertEqual(code, 0)
        lines = [json.loads(o) for o in out]
        start, last = lines[0], lines[-1]
        run_id = start["run_id"]
        self.assertRegex(run_id, r"^\d{8}T\d{12}Z-[0-9a-f]{4}$")
        self.assertEqual(last["action"], "audit_written")
        key = last["key"]
        self.assertEqual(key, "audit/promoter/%s/%s/%s/%s.jsonl"
                         % (run_id[:4], run_id[4:6], run_id[6:8], run_id))
        self.assertEqual(self.audit_keys(), [key])
        stored = self.s3.get_object(Bucket="crsw", Key=key)["Body"].read().decode()
        stored_lines = [json.loads(l) for l in stored.splitlines()]
        # Everything up to and including finish, and nothing after.
        self.assertEqual(stored_lines, lines[:-1])
        self.assertEqual(stored_lines[-1]["action"], "finish")
        self.assertIn("promoted", [l["action"] for l in stored_lines])

    def test_dry_run_and_recategorise_are_on_the_record_too(self):
        self.stage()
        self.cli(["run", "--dry-run"])
        self.cli(["recategorise", "--dry-run"])
        keys_ = self.audit_keys()
        self.assertEqual(len(keys_), 2)
        firsts = [json.loads(self.s3.get_object(Bucket="crsw", Key=k)["Body"]
                             .read().decode().splitlines()[0]) for k in keys_]
        self.assertTrue(all(f["dry_run"] for f in firsts))
        self.assertEqual(sorted(f.get("command", "run") for f in firsts),
                         ["recategorise", "run"])

    def test_blank_prefix_disables_the_trail(self):
        self.stage()
        code, out = self.cli(["run"], env={"PROMOTER_AUDIT_PREFIX": ""})
        self.assertEqual(code, 0)
        self.assertEqual(self.audit_keys(), [])
        self.assertEqual(json.loads(out[-1])["action"], "finish")

    def test_real_run_whose_trail_cannot_be_written_exits_2(self):
        self.stage()
        with mock.patch("promoter.audit.write_run", side_effect=OSError("bucket gone")):
            code, out = self.cli(["run"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out[-1])["action"], "audit_write_failed")
        # ...but the promotion itself happened and is in the printed log.
        self.assertIn("promoted", [json.loads(o)["action"] for o in out])
        with mock.patch("promoter.audit.write_run", side_effect=OSError("bucket gone")):
            code, out = self.cli(["run", "--dry-run"])
        self.assertEqual(code, 0)      # a dry run is not made to fail by this

    def test_audit_command_reads_runs_back_newest_first_with_filters(self):
        a = self.stage()
        b = self.stage(dataset="promo-b")
        self.cli(["run", "--deposit", a.id])
        self.cli(["run", "--deposit", b.id])
        code, out = self.cli(["audit", "--json"])
        self.assertEqual(code, 0)
        entries = [json.loads(o) for o in out]
        # newest run first, each run in order
        runs = [e["run_id"] for e in entries if e["action"] == "start"]
        self.assertEqual(runs, sorted(runs, reverse=True))
        self.assertEqual(entries[0]["action"], "start")
        self.assertEqual([e["deposit"] for e in entries if e["action"] == "promoted"],
                         [b.id, a.id])
        # filters
        code, out = self.cli(["audit", "--json", "--dataset", "promo-b"])
        promoted = [json.loads(o) for o in out if json.loads(o)["action"] == "promoted"]
        self.assertEqual([e["deposit"] for e in promoted], [b.id])
        code, out = self.cli(["audit", "--json", "--action", "promoted", "--user", "k1078591"])
        self.assertEqual(len(out), 2)
        code, out = self.cli(["audit", "--json", "--user", "nobody"])
        self.assertEqual(out, [])
        code, out = self.cli(["audit", "--runs", "1", "--json"])
        self.assertEqual(len([o for o in out if '"start"' in o]), 1)
        code, out = self.cli(["audit", "--since", "2099-01-01"])
        self.assertEqual(out, ["(no matching audit lines)"])

    def test_audit_lines_read_as_words(self):
        dep = self.stage()
        self.cli(["run"])
        code, out = self.cli(["audit", "--action", "promoted"])
        self.assertEqual(len(out), 1)
        line = out[0]
        self.assertIn("promoted", line)
        self.assertIn(DEST, line)
        self.assertIn("by k1078591", line)
        self.assertIn("deposit " + dep.id, line)
        code, out = self.cli(["audit", "--action", "checked"])
        self.assertIn("bytes=", out[0])
        self.assertIn(" B", out[0])       # human-readable size

    def test_datasets_lists_records_in_place(self):
        self.stage()
        self.cli(["run"])
        code, out = self.cli(["datasets", "--json"])
        self.assertEqual(code, 0)
        rows = [json.loads(o) for o in out]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["prefix"], DEST)
        self.assertEqual(r["identifier"], DEST)
        self.assertEqual(r["depositor"], "k1078591")
        self.assertEqual(r["files"], 2)
        self.assertEqual(r["bytes"], 11)
        self.assertEqual(r["schema_version"], "0.6")
        self.assertRegex(r["dataset_uuid"], r"^[0-9a-f-]{36}$")
        code, out = self.cli(["datasets"])
        header, row = out[0].splitlines()
        self.assertTrue(header.startswith("prefix"))
        self.assertIn(DEST, row)
        code, out = self.cli(["datasets", "--strand", "rs1"])
        self.assertEqual(out, ["(nothing in place)"])
        code, out = self.cli(["datasets", "--strand", "rs9"])
        self.assertEqual(code, 2)
