"""crsw_web.bulk: a prepared dataset staged as the web route would leave
it, against moto's S3."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import boto3
    from moto import mock_aws
    from crsw_web.bulk import COPIED, UPLOADED, BulkError, StagingInterrupted, stage_dataset
    from crsw_web.deposits import CONTROL_NAME, STATUS_COMPLETE, DepositStore
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import keys, labels, record

IDENT = "centre/cdisaw/green/2_final/demo"
UUID = "2f1c3b9a-5d6e-4f70-8a1b-2c3d4e5f6a7b"
NOW = "2026-10-23T12:00:00Z"


def make_record(files):
    entries = [record.manifest_entry(p, hashlib.sha256(b).hexdigest(), len(b))
               for p, b in sorted(files.items())]
    return record.build_record(
        UUID, IDENT, "centre", ["quant"], "cdisaw", "demo", "2_final", "green",
        {"start": "1990", "end": "2020"}, "1-0", "A demo dataset.", ["armed-conflict"],
        entries, NOW, NOW, depositors=["k1078591"])


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestStageDataset(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")
        self.store = DepositStore(self.s3, "crsw", "staging")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = {"wide/events/demo.parquet": b"PAR1 events",
                     "wide/status/demo.json": b'{"admin0_codes": ["GBR"]}'}
        for p, b in self.data.items():
            f = self.root.joinpath(*p.split("/"))
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b)
        self.files = {p: self.root.joinpath(*p.split("/")) for p in self.data}
        self.rec = make_record(self.data)

    def keys_under(self, prefix):
        return sorted(o["Key"] for o in self.s3.list_objects_v2(
            Bucket="crsw", Prefix=prefix).get("Contents", []))

    def test_stages_members_record_and_complete_control_object(self):
        out = stage_dataset(self.store, "k1078591", self.rec, self.files, NOW)
        dep = out.deposit
        root = "staging/k1078591/%s/" % dep.id
        self.assertEqual(self.keys_under(root), sorted([
            root + CONTROL_NAME,
            root + IDENT + "/dataset.demo.json",
            root + IDENT + "/wide/events/demo.parquet",
            root + IDENT + "/wide/status/demo.json"]))
        for p, b in self.data.items():
            head = self.s3.head_object(Bucket="crsw", Key=root + IDENT + "/" + p)
            self.assertEqual(head["Metadata"], labels.object_labels(
                UUID, hashlib.sha256(b).hexdigest(), "green", "k1078591"))
        rec_obj = self.s3.get_object(Bucket="crsw", Key=out.record_key)
        body = rec_obj["Body"].read()
        self.assertEqual(rec_obj["Metadata"][labels.CHECKSUM], hashlib.sha256(body).hexdigest())
        self.assertEqual(json.loads(body)["identifier"], IDENT)
        control = json.loads(self.s3.get_object(
            Bucket="crsw", Key=root + CONTROL_NAME)["Body"].read())
        self.assertEqual(control["status"], STATUS_COMPLETE)
        self.assertEqual(control["record_key"], out.record_key)
        self.assertEqual(control["prefix"], IDENT)
        self.assertEqual([e["path"] for e in control["entries"]], sorted(self.data))
        self.assertEqual({a for _, a, _ in out.members}, {UPLOADED})

    def test_the_promoter_rebuilds_the_same_record_from_the_control_object(self):
        # promoter/promote.py assembles the record from dep.meta and dep.entries;
        # it must come out as the record that was staged.
        from crsw_deposit import deposit_logic
        dep = stage_dataset(self.store, "k1078591", self.rec, self.files, NOW).deposit
        rebuilt, _, _, _ = deposit_logic.assemble_record(
            dep.meta, None, dep.entries, dep.user, NOW, dep.dataset_uuid, created=NOW)
        self.assertEqual(rebuilt, self.rec)

    def test_path_mismatch_refused_before_any_write(self):
        files = dict(self.files)
        files.pop("wide/status/demo.json")
        with self.assertRaises(BulkError):
            stage_dataset(self.store, "k1078591", self.rec, files, NOW)
        self.assertEqual(self.keys_under("staging/"), [])

    def test_size_mismatch_refused(self):
        self.files["wide/events/demo.parquet"].write_bytes(b"different length")
        with self.assertRaises(BulkError):
            stage_dataset(self.store, "k1078591", self.rec, self.files, NOW)

    def test_a_file_absent_on_disk_is_reported_as_missing_not_a_traceback(self):
        self.files["wide/status/demo.json"].unlink()
        with self.assertRaises(BulkError) as cm:
            stage_dataset(self.store, "k1078591", self.rec, self.files, NOW)
        self.assertIn("missing locally ['wide/status/demo.json']", str(cm.exception))
        self.assertEqual(self.keys_under("staging/"), [])

    def test_an_interrupted_upload_names_the_open_deposit(self):
        # The control object is created before the first member; if the
        # network drops after that, the operator must learn the id of the
        # open deposit to remove it (it is never promoted).
        calls = []

        def drop(Filename, Bucket, Key, ExtraArgs=None):
            calls.append(Key)
            if len(calls) == 2:
                raise ConnectionError("VPN dropped")
            self.s3.put_object(Bucket=Bucket, Key=Key, Body=Path(Filename).read_bytes(),
                               Metadata=ExtraArgs["Metadata"])
        with mock.patch.object(self.s3, "upload_file", side_effect=drop):
            with self.assertRaises(StagingInterrupted) as cm:
                stage_dataset(self.store, "k1078591", self.rec, self.files, NOW)
        err = cm.exception
        [dep_id] = [k.split("/")[2] for k in self.keys_under("staging/") if k.endswith(CONTROL_NAME)]
        self.assertEqual((err.user, err.deposit_id), ("k1078591", dep_id))
        self.assertIsInstance(err.cause, ConnectionError)
        self.assertIn("remove --staging %s --confirm %s" % (dep_id, dep_id), str(err))
        self.assertIsInstance(err, BulkError)
        control = json.loads(self.s3.get_object(
            Bucket="crsw", Key="staging/k1078591/%s/%s" % (dep_id, CONTROL_NAME))["Body"].read())
        self.assertNotEqual(control["status"], STATUS_COMPLETE)

    def test_user_must_be_a_depositor(self):
        with self.assertRaises(BulkError):
            stage_dataset(self.store, "k0000000", self.rec, self.files, NOW)

    def test_dry_run_writes_nothing(self):
        out = stage_dataset(self.store, "k1078591", self.rec, self.files, NOW, dry_run=True)
        self.assertIsNone(out.deposit)
        self.assertEqual(len(out.members), 2)
        self.assertEqual(out.bytes, sum(len(b) for b in self.data.values()))
        self.assertEqual(self.keys_under(""), [])

    def test_copy_from_copies_same_size_and_uploads_otherwise(self):
        self.s3.put_object(Bucket="crsw", Key="cdisaw-test/wide/events/demo.parquet",
                           Body=self.data["wide/events/demo.parquet"],
                           Metadata={"stale": "label"})
        self.s3.put_object(Bucket="crsw", Key="cdisaw-test/wide/status/demo.json",
                           Body=b"wrong size")
        dry = stage_dataset(self.store, "k1078591", self.rec, self.files, NOW,
                            copy_from="cdisaw-test", dry_run=True)
        self.assertEqual(dry.copied, 1)
        out = stage_dataset(self.store, "k1078591", self.rec, self.files, NOW,
                            copy_from="cdisaw-test")
        actions = {p: a for p, a, _ in out.members}
        self.assertEqual(actions, {"wide/events/demo.parquet": COPIED,
                                   "wide/status/demo.json": UPLOADED})
        root = "staging/k1078591/%s/%s/" % (out.deposit.id, IDENT)
        head = self.s3.head_object(Bucket="crsw", Key=root + "wide/events/demo.parquet")
        self.assertNotIn("stale", head["Metadata"])
        self.assertEqual(head["Metadata"][labels.DATASET_UUID], UUID)
        got = self.s3.get_object(Bucket="crsw", Key=root + "wide/status/demo.json")["Body"].read()
        self.assertEqual(got, self.data["wide/status/demo.json"])


class TestCentreStrand(unittest.TestCase):
    """r12: the schema and keys accept `centre`; deposits are not offered it."""

    def test_centre_key_builds_and_record_validates(self):
        self.assertIn("centre", keys.STRANDS)
        self.assertNotIn("centre", keys.DEPOSIT_STRANDS)
        self.assertEqual(keys.dataset_prefix("centre", "cdisaw", "green", "2_final", "demo"), IDENT)
        rec = make_record({"a.parquet": b"x"})
        errors, _ = record.validate_record(rec, {"armed-conflict"})
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
