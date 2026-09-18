"""End-to-end against the real Ceph cluster, under staging/_test/.

Opt-in only: needs CRSW_INTEGRATION=1, a .env with the service key, and
the KCL VPN. Everything it writes is deleted afterwards. Run with:

    CRSW_INTEGRATION=1 .venv/Scripts/python -m pytest tests/test_integration_cluster.py -q
"""
import hashlib
import os
import unittest

try:
    from fastapi.testclient import TestClient
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.config import ConfigError, Settings, load_dotenv
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import record

ENABLED = os.environ.get("CRSW_INTEGRATION") == "1"

FORM = {
    "strand": "rs2", "sensitivity": "green", "state": "0_raw",
    "project": "csac", "dataset": "web-integration", "domain": "quant",
    "version": "1-0", "coverage_start": "2020", "coverage_end": "2021",
    "subject": ["forced-labour"],
    "abstract": ("Integration test deposit written by the web service test "
                 "suite against the staging area; safe to delete. ") * 4,
}


@unittest.skipUnless(HAVE_WEB and ENABLED, "set CRSW_INTEGRATION=1 to run")
class TestAgainstCluster(unittest.TestCase):
    def setUp(self):
        load_dotenv()
        try:
            self.settings = Settings.from_env()
        except ConfigError as e:
            self.skipTest(str(e))
        self.s3 = s3mod.make_client(self.settings)
        self.client = TestClient(create_app(self.settings, s3_client=self.s3))
        self.written = []

    def tearDown(self):
        for key in self.written:
            try:
                self.s3.delete_object(Bucket=self.settings.s3_bucket, Key=key)
            except Exception:
                pass

    def test_two_file_deposit(self):
        d = self.client.post("/deposits", json=FORM).json()
        root = "%s/%s/%s" % (self.settings.staging_prefix,
                             self.settings.dev_user, d["id"])
        self.written.append(root + "/_deposit.json")

        small = b"a,b\n1,2\n"
        big = os.urandom(9 * 1024 * 1024)      # forces the multipart path
        for member, data in (("one.csv", small), ("sub/blob.bin", big)):
            r = self.client.put("/deposits/%s/files/%s" % (d["id"], member),
                                content=data)
            self.assertEqual(r.status_code, 200, r.text)
            self.written.append(r.json()["staged_key"])
            head = self.s3.head_object(Bucket=self.settings.s3_bucket,
                                       Key=r.json()["staged_key"])
            self.assertEqual(head["ContentLength"], len(data))
            self.assertEqual(head["Metadata"]["checksum-sha256"],
                             hashlib.sha256(data).hexdigest())
            self.assertEqual(head["Metadata"]["depositor"], self.settings.dev_user)

        r = self.client.post("/deposits/%s/finalise" % d["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.written.append(r.json()["record_key"])
        body = self.s3.get_object(Bucket=self.settings.s3_bucket,
                                  Key=r.json()["record_key"])["Body"].read()
        rec = record.parse_record(body.decode("utf-8"))
        self.assertEqual(len(rec["files"]), 2)
        self.assertEqual(rec["dataset_uuid"], d["dataset_uuid"])
