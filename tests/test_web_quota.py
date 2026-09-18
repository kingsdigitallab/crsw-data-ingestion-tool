import json
import os
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
    from crsw_web import quota
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
BASE = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
            s3_bucket="crsw", staging_prefix="staging/_test")
FORM = {"strand": "rs2", "sensitivity": "green", "state": "0_raw", "project": "csac",
        "dataset": "q", "domain": "quant", "version": "1-0", "coverage_start": "2020",
        "coverage_end": "2021", "subject": ["forced-labour"], "abstract": "w " * 60}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestQuota(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.mock = mock_aws(); self.mock.start(); self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")
        self.store = DepositStore(self.s3, "crsw", "staging/_test")

    def client(self, **overrides):
        return TestClient(create_app(Settings(**dict(BASE, **overrides)),
                                     s3_client=self.s3, vocab_dict=VOCAB))

    def test_usage_counts_bytes_and_open_deposits(self):
        c = self.client()
        a = c.post("/deposits", json=FORM).json()
        c.put("/deposits/%s/files/x.bin" % a["id"], content=b"x" * 100)
        b = c.post("/deposits", json=dict(FORM, dataset="q2")).json()
        c.put("/deposits/%s/files/y.bin" % b["id"], content=b"y" * 50)
        c.post("/deposits/%s/finalise" % b["id"])
        use = quota.usage(self.store, "k1078591")
        self.assertEqual(use.open_deposits, 1)
        self.assertEqual(use.deposits, 2)
        # 150 bytes of members plus the finalised record's bytes; control objects excluded.
        self.assertGreater(use.bytes_in_staging, 150)

    def test_declared_length_over_remaining_quota_is_413(self):
        c = self.client(user_quota_bytes=120)
        d = c.post("/deposits", json=FORM).json()
        self.assertEqual(c.put("/deposits/%s/files/a" % d["id"], content=b"x" * 100).status_code, 200)
        r = c.put("/deposits/%s/files/b" % d["id"], content=b"y" * 30)
        self.assertEqual(r.status_code, 413)
        self.assertIn("quota", r.json()["detail"])
        self.assertEqual(c.put("/deposits/%s/files/c" % d["id"], content=b"z" * 20).status_code, 200)

    def test_streamed_body_over_quota_is_cut_off(self):
        # No Content-Length: the running total is capped instead.
        c = self.client(user_quota_bytes=50)
        d = c.post("/deposits", json=FORM).json()

        def body():
            yield b"x" * 40
            yield b"x" * 40
        r = c.put("/deposits/%s/files/a" % d["id"], content=body())
        self.assertEqual(r.status_code, 413)

    def test_open_deposit_cap(self):
        c = self.client(user_max_open_deposits=2)
        c.post("/deposits", json=FORM)
        c.post("/deposits", json=dict(FORM, dataset="q2"))
        r = c.post("/deposits", json=dict(FORM, dataset="q3"))
        self.assertEqual(r.status_code, 429)
        self.assertIn("still open", r.json()["detail"])

    def test_member_cap(self):
        c = self.client(max_members_per_deposit=1)
        d = c.post("/deposits", json=FORM).json()
        self.assertEqual(c.put("/deposits/%s/files/a" % d["id"], content=b"1").status_code, 200)
        self.assertEqual(c.put("/deposits/%s/files/a" % d["id"], content=b"2").status_code, 200)  # replace ok
        self.assertEqual(c.put("/deposits/%s/files/b" % d["id"], content=b"3").status_code, 413)

    def test_unlimited_by_default(self):
        c = self.client()
        d = c.post("/deposits", json=FORM).json()
        self.assertEqual(c.put("/deposits/%s/files/a" % d["id"], content=b"x" * 5000).status_code, 200)
