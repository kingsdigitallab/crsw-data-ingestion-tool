import json
import os
import shutil
import subprocess
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
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import noise, vocab

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t",
                s3_secret_key="t", s3_bucket="crsw", staging_prefix="staging/_test")
FORM = {
    "strand": "rs2", "sensitivity": "green", "state": "0_raw",
    "project": "csac", "dataset": "poc-form", "domain": "quant",
    "version": "1-0", "coverage_start": "2020", "coverage_end": "2021",
    "subject": ["forced-labour"], "abstract": "words " * 60,
}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestForm(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.mock = mock_aws(); self.mock.start(); self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")
        self.client = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3,
                                            vocab_dict=VOCAB))

    def test_form_renders_from_the_vocabulary(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        html = r.text
        for s in ("rs1", "rs2", "rs3", "rs4"):
            self.assertIn('value="%s"' % s, html)
        self.assertIn("Kevin Fahey", html)                 # steward on the domain option
        self.assertIn('value="forced-labour"', html)       # a subject checkbox
        self.assertIn("Depositing as <strong>k1078591", html)
        self.assertIn("/static/deposit.js", html)

    def test_origin_section_renders_with_the_activity_vocabulary(self):
        html = self.client.get("/").text
        self.assertIn('name="derived_from"', html)
        self.assertIn('name="provenance_tool"', html)
        self.assertIn('name="provenance_commit"', html)
        for a in vocab.activities(VOCAB):
            self.assertIn('value="%s"' % a["code"], html)
        # No read role: no picker, and nothing in the page calls /datasets.json.
        self.assertNotIn('id="picker"', html)

    def test_picker_appears_with_the_read_role(self):
        client = TestClient(create_app(
            Settings(**dict(SETTINGS, read_s3_access_key="r", read_s3_secret_key="r")),
            s3_client=self.s3, vocab_dict=VOCAB, read_client=object()))
        html = client.get("/").text
        self.assertIn('id="picker"', html)
        self.assertIn("Find a dataset in the store", html)
        self.assertIn('href="/datasets"', html)
        js = client.get("/static/deposit.js").text
        self.assertIn("/datasets.json?limit=20&q=", js)

    def test_rules_embedded_from_crsw_deposit(self):
        html = self.client.get("/").text
        start = html.index('<script id="rules" type="application/json">') + len(
            '<script id="rules" type="application/json">')
        rules = json.loads(html[start:html.index("</script>", start)])
        self.assertEqual(set(rules["noise_file_names"]), set(noise.NOISE_FILE_NAMES))
        self.assertEqual(rules["noise_file_prefixes"], ["._"])

    def test_script_parses(self):
        """The whole form dies if deposit.js has a syntax error (seen once:
        a patch turned regex escapes into line breaks). Parse-check it
        with Node when Node is installed."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        js = self.client.get("/static/deposit.js").text
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(js)
            run = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
        finally:
            os.unlink(path)

    def test_static_served(self):
        self.assertEqual(self.client.get("/static/deposit.css").status_code, 200)
        self.assertEqual(self.client.get("/static/deposit.js").status_code, 200)

    def test_summary_page_after_a_deposit(self):
        d = self.client.post("/deposits", json=FORM).json()
        self.client.put("/deposits/%s/files/one.csv" % d["id"], content=b"a,b\n")
        self.client.post("/deposits/%s/finalise" % d["id"])
        r = self.client.get("/deposits/%s/summary" % d["id"])
        self.assertEqual(r.status_code, 200)
        self.assertIn("Deposited", r.text)
        self.assertIn("dataset.poc-form.json", r.text)
        self.assertIn('&#34;schema_version&#34;: &#34;0.6&#34;', r.text)  # HTML-escaped in <pre>

    def test_summary_404_for_unknown(self):
        self.assertEqual(self.client.get("/deposits/nope/summary").status_code, 404)
