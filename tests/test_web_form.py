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

from crsw_deposit import authority, noise, vocab

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

    def test_form_offers_providers_places_and_the_r11_fields(self):
        html = self.client.get("/").text
        # Provider is a type-ahead with pills too, over the vocabulary's list.
        self.assertIn('id="provider-search"', html)
        self.assertIn('id="provider-pills"', html)
        self.assertNotIn('name="provider" multiple', html)
        start = html.index('<script id="providers" type="application/json">') + len(
            '<script id="providers" type="application/json">')
        providers = json.loads(html[start:html.index("</script>", start)])
        acled = next(p for p in providers if p["slug"] == "acled")
        self.assertEqual(acled["name"], "Armed Conflict Location & Event Data (ACLED)")
        self.assertTrue(acled["rights"].startswith("ACLED terms of use"))
        self.assertEqual(len(providers), 6)
        self.assertIn('name="provider_other"', html)
        # Spatial coverage is a type-ahead over the places in the page, with
        # the chosen ones as pills; no multi-select.
        self.assertIn('id="place-search"', html)
        self.assertIn('id="place-pills"', html)
        self.assertNotIn('name="spatial" multiple', html)
        start = html.index('<script id="places" type="application/json">') + len(
            '<script id="places" type="application/json">')
        places = json.loads(html[start:html.index("</script>", start)])
        by_code = {p["code"]: p for p in places}
        self.assertEqual(by_code["iso:CO"]["label"], "Colombia")
        self.assertEqual(by_code["iso:CO"]["path"],
                         ["South America", "Latin America and the Caribbean", "Americas", "World"])
        self.assertEqual(by_code["m49:001"]["path"], [])
        self.assertEqual(len(places), 278)
        self.assertIn('name="spatial_note"', html)
        self.assertIn('name="notes"', html)
        self.assertIn('name="domain_extra" value="geo"', html)
        self.assertIn('id="creator-hint"', html)

    def test_header_and_health_show_the_release_the_image_was_built_from(self):
        with mock.patch.dict(os.environ, {"CRSW_VERSION": "v9.9.9"}):
            c = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3, vocab_dict=VOCAB))
        self.assertIn('<span class="release" title="the release this service was built from">'
                      'v9.9.9</span>', c.get("/").text)
        self.assertEqual(c.get("/health").json()["release"], "v9.9.9")
        from crsw_web import version
        self.assertEqual(version.release({"CRSW_VERSION": "v1.2.3"}), "v1.2.3")
        # Blank or the Dockerfile's default: the checkout's git describe, else "dev".
        self.assertTrue(version.release({"CRSW_VERSION": "unknown"}))

    def test_spatial_sits_with_temporal_coverage_and_origin_is_derivation(self):
        html = self.client.get("/").text
        self.assertLess(html.index('id="coverage_end"'), html.index('id="place-search"'))
        self.assertLess(html.index('id="place-search"'), html.index('name="subject"'))
        self.assertIn("Derivation: what this was made from, and what made it", html)
        self.assertNotIn("Origin:", html)
        self.assertIn('id="origin"', html)          # the script's hook is unchanged

    def test_the_form_terms_are_built_once_per_vocabulary(self):
        from crsw_deposit import authority as auth_mod
        c = self.client
        with mock.patch("crsw_web.app.authority.tree", wraps=auth_mod.tree) as tree:
            self.assertEqual(c.get("/").status_code, 200)
            first = tree.call_count
            self.assertGreater(first, 0)
            self.assertEqual(c.get("/").status_code, 200)
            self.assertEqual(tree.call_count, first)        # not rebuilt
            c.app.state.vocab_cache._set(dict(VOCAB), "remote")  # a refresh swapped the doc
            self.assertEqual(c.get("/").status_code, 200)
            self.assertEqual(tree.call_count, 2 * first)    # rebuilt once

    def test_scope_note_renders_as_hover_text_and_an_expand_control(self):
        described = authority.describe_term(
            VOCAB, "forced-labour", scope='Work exacted under the menace of a "penalty".')
        client = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3,
                                       vocab_dict=described))
        html = client.get("/").text
        note = "Work exacted under the menace of a &#34;penalty&#34;."
        self.assertIn('<label class="depth-0" title="%s">' % note, html)
        self.assertIn('<details class="scope depth-0"><summary>what this covers</summary>'
                      '<p>%s</p></details>' % note, html)
        self.assertEqual(html.count('<details class="scope'), 1)   # only the described term
        self.assertIn('value="forced-labour"> forced-labour</label>', html)   # text is the slug
        # A vocabulary without scope notes renders exactly as before.
        plain = self.client.get("/").text
        self.assertNotIn('<details class="scope', plain)
        self.assertNotIn("title=", plain.split('data-field="subject"')[1].split("</fieldset>")[0])

    def test_origin_section_renders_with_the_activity_vocabulary(self):
        html = self.client.get("/").text
        self.assertIn('name="derived_from"', html)
        self.assertIn('name="provenance_tool"', html)
        self.assertIn('name="provenance_commit"', html)
        for a in vocab.activities(VOCAB):
            self.assertIn('value="%s"' % a["code"], html)
        # No read role: no lookup panel on the page.
        self.assertNotIn('id="lookup"', html)

    def test_picker_appears_with_the_read_role(self):
        client = TestClient(create_app(
            Settings(**dict(SETTINGS, read_s3_access_key="r", read_s3_secret_key="r")),
            s3_client=self.s3, vocab_dict=VOCAB, read_client=object()))
        html = client.get("/").text
        self.assertIn('id="lookup"', html)
        self.assertIn("Find a dataset in the store", html)
        self.assertIn('<label for="lookup-q">Search the store</label>', html)
        self.assertIn('id="lookup-close">Close</button>', html)
        self.assertNotIn('class="picker" id=', html)   # the file-chooser class is not reused
        self.assertIn('href="/datasets"', html)
        js = client.get("/static/deposit.js").text
        self.assertIn("/datasets.json?limit=20&q=", js)

    def test_existing_dataset_help_appears_with_the_read_role(self):
        client = TestClient(create_app(
            Settings(**dict(SETTINGS, read_s3_access_key="r", read_s3_secret_key="r")),
            s3_client=self.s3, vocab_dict=VOCAB, read_client=object()))
        html = client.get("/").text
        for marker in ('id="destination"', 'id="confirm-existing"', 'id="use-details"',
                       'list="project-options"', 'list="dataset-options"',
                       'id="restricted"', "restricting beyond the strand"):
            self.assertIn(marker, html, marker)
        js = client.get("/static/deposit.js").text
        for marker in ("/deposits/lookup?", "confirm_existing", "unchanged", "merges_with"):
            self.assertIn(marker, js, marker)
        # Without the read role the form is as it was.
        html = self.client.get("/").text
        for marker in ('id="destination"', 'id="confirm-existing"', 'list="project-options"',
                       'id="restricted"'):
            self.assertNotIn(marker, html, marker)

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
        self.assertIn('&#34;schema_version&#34;: &#34;0.7&#34;', r.text)  # HTML-escaped in <pre>

    def test_summary_404_for_unknown(self):
        self.assertEqual(self.client.get("/deposits/nope/summary").status_code, 404)
