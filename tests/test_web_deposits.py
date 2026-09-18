import hashlib
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
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import deposit_logic, record

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))

SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t",
                s3_secret_key="t", s3_bucket="crsw", staging_prefix="staging/_test")

FORM = {
    "strand": "rs2", "sensitivity": "green", "state": "0_raw",
    "project": "csac", "dataset": "poc-test", "domain": "quant",
    "version": "1-0", "coverage_start": "2020", "coverage_end": "2021",
    "subject": ["forced-labour"], "abstract": "words " * 60,
    "source_type": "archive", "source_detail": "The National Archives, CO 123",
    "creator": "Neil",
}
ROOT = "staging/_test/k1078591"


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestDepositFlow(unittest.TestCase):
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
        self.settings = Settings(**SETTINGS)
        self.client = TestClient(create_app(self.settings, s3_client=self.s3,
                                            vocab_dict=VOCAB))


    def create(self, **overrides):
        r = self.client.post("/deposits", json=dict(FORM, **overrides))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def put(self, dep_id, member, data, **params):
        return self.client.put("/deposits/%s/files/%s" % (dep_id, member),
                               content=data, params=params)

    def head(self, key):
        return self.s3.head_object(Bucket="crsw", Key=key)

    # --- create -----------------------------------------------------------
    def test_create_returns_prefixes_and_writes_control_object(self):
        d = self.create()
        self.assertEqual(d["prefix"], "rs2/csac/green/0_raw/poc-test")
        self.assertEqual(d["staging_prefix"],
                         "%s/%s/rs2/csac/green/0_raw/poc-test" % (ROOT, d["id"]))
        self.head("%s/%s/_deposit.json" % (ROOT, d["id"]))
        state = self.client.get("/deposits/%s" % d["id"]).json()
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["entries"], [])
        self.assertEqual(state["meta"]["steward"], "Kevin Fahey")

    def test_create_field_errors_are_422_with_inline_messages(self):
        r = self.client.post("/deposits", json=dict(FORM, sensitivity="red",
                                                    subject=[]))
        self.assertEqual(r.status_code, 422)
        errors = r.json()["detail"]["errors"]
        self.assertIn("TRE", errors["sensitivity"])
        self.assertIn("subject", errors)

    def test_create_short_abstract_is_a_warning(self):
        d = self.create(abstract="short")
        self.assertTrue(any("Abstract" in w for w in d["warnings"]))

    # --- files ------------------------------------------------------------
    def test_put_file_streams_with_labels_and_records_entry(self):
        d = self.create()
        data = b"a,b\n1,2\n"
        r = self.put(d["id"], "one.csv", data)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["key"], "rs2/csac/green/0_raw/poc-test/one.csv")
        self.assertEqual(body["bytes"], len(data))
        self.assertEqual(body["checksum_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(body["format"], "text/csv")
        head = self.head(body["staged_key"])
        self.assertEqual(head["Metadata"], {
            "dataset-uuid": d["dataset_uuid"],
            "checksum-sha256": body["checksum_sha256"],
            "sensitivity": "green", "depositor": "k1078591"})
        state = self.client.get("/deposits/%s" % d["id"]).json()
        self.assertEqual([e["path"] for e in state["entries"]], ["one.csv"])

    def test_nested_member_keeps_subpath(self):
        d = self.create()
        r = self.put(d["id"], "2024/tiles/a.tif", b"x")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["key"],
                         "rs2/csac/green/0_raw/poc-test/2024/tiles/a.tif")

    def test_reput_replaces_entry(self):
        d = self.create()
        self.put(d["id"], "one.csv", b"v1")
        self.put(d["id"], "one.csv", b"version two")
        state = self.client.get("/deposits/%s" % d["id"]).json()
        self.assertEqual(len(state["entries"]), 1)
        self.assertEqual(state["entries"][0]["bytes"], 11)

    def test_reserved_and_bad_member_paths_refused(self):
        d = self.create()
        self.assertEqual(self.put(d["id"], "dataset.x.json", b"").status_code, 422)
        self.assertEqual(self.put(d["id"], "sub/dataset.x.json", b"").status_code, 422)
        # %5C is a backslash once the router decodes it.
        self.assertEqual(self.put(d["id"], "bad%5Cpath", b"").status_code, 422)

    def test_noise_refused_unless_included(self):
        d = self.create()
        r = self.put(d["id"], "sub/.DS_Store", b"")
        self.assertEqual(r.status_code, 422)
        self.assertIn("include_noise", r.json()["detail"])
        r = self.put(d["id"], "sub/.DS_Store", b"", include_noise=1)
        self.assertEqual(r.status_code, 200)

    def test_body_over_cap_refused(self):
        settings = Settings(**dict(SETTINGS, max_body_bytes=10))
        client = TestClient(create_app(settings, s3_client=self.s3, vocab_dict=VOCAB))
        d = client.post("/deposits", json=FORM).json()
        r = client.put("/deposits/%s/files/big.bin" % d["id"], content=b"x" * 11)
        self.assertEqual(r.status_code, 413)

    def test_delete_file(self):
        d = self.create()
        staged = self.put(d["id"], "one.csv", b"x").json()["staged_key"]
        r = self.client.delete("/deposits/%s/files/one.csv" % d["id"])
        self.assertEqual(r.status_code, 204)
        with self.assertRaises(self.s3.exceptions.ClientError):
            self.head(staged)
        self.assertEqual(self.client.get("/deposits/%s" % d["id"]).json()["entries"], [])

    # --- finalise ---------------------------------------------------------
    def test_full_flow_record_is_what_the_cli_would_write(self):
        d = self.create()
        self.put(d["id"], "sub/readme.md", b"hello\n")
        self.put(d["id"], "one.csv", b"a,b\n1,2\n")
        r = self.client.post("/deposits/%s/finalise" % d["id"])
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["files"], 2)
        self.assertEqual(out["record_key"],
                         "%s/%s/rs2/csac/green/0_raw/poc-test/dataset.poc-test.json"
                         % (ROOT, d["id"]))

        stored = self.s3.get_object(Bucket="crsw", Key=out["record_key"])
        data = stored["Body"].read()
        rec = record.parse_record(data.decode("utf-8"))
        # Byte-for-byte what deposit_logic produces for the same inputs.
        state = self.client.get("/deposits/%s" % d["id"]).json()
        expected, _, _, _ = deposit_logic.assemble_record(
            state["meta"], None, state["entries"], "k1078591",
            rec["modified"], d["dataset_uuid"])
        self.assertEqual(data, deposit_logic.record_bytes(expected))
        self.assertNotIn(b"\r", data)
        self.assertEqual([e["path"] for e in rec["files"]],
                         ["one.csv", "sub/readme.md"])
        self.assertEqual(rec["depositors"], ["k1078591"])
        self.assertEqual(rec["identifier"], "rs2/csac/green/0_raw/poc-test")

        head = self.head(out["record_key"])
        self.assertEqual(head["Metadata"]["checksum-sha256"],
                         hashlib.sha256(data).hexdigest())
        self.assertEqual(head["Metadata"]["dataset-uuid"], d["dataset_uuid"])

        self.assertEqual(state["status"], "complete")
        # Second finalise and further PUTs are refused.
        self.assertEqual(self.client.post("/deposits/%s/finalise" % d["id"]).status_code, 409)
        self.assertEqual(self.put(d["id"], "late.txt", b"x").status_code, 409)

    def test_finalise_with_nothing_is_409(self):
        d = self.create()
        self.assertEqual(self.client.post("/deposits/%s/finalise" % d["id"]).status_code, 409)

    def test_finalise_detects_object_missing_behind_its_back(self):
        d = self.create()
        staged = self.put(d["id"], "one.csv", b"x").json()["staged_key"]
        self.s3.delete_object(Bucket="crsw", Key=staged)
        r = self.client.post("/deposits/%s/finalise" % d["id"])
        self.assertEqual(r.status_code, 409)
        self.assertIn("no object at", r.json()["detail"]["problems"][0])

    def test_other_user_cannot_see_the_deposit(self):
        d = self.create()
        other = TestClient(create_app(Settings(**dict(SETTINGS, dev_user="bob")),
                                      s3_client=self.s3, vocab_dict=VOCAB))
        self.assertEqual(other.get("/deposits/%s" % d["id"]).status_code, 404)
        self.assertEqual(other.put("/deposits/%s/files/x" % d["id"],
                                   content=b"x").status_code, 404)

    def test_vocabulary_endpoint(self):
        body = self.client.get("/vocabulary").json()
        self.assertIn("practices", body["facets"])
        self.assertEqual(body["strands"], ["rs1", "rs2", "rs3", "rs4"])
