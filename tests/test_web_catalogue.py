"""The read role: find and browse what is in the store through the
read-only key and the promoter's index (finding-and-reuse.md §§1-3)."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

try:
    import boto3
    from fastapi.testclient import TestClient
    from moto import mock_aws
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.catalogue import Catalogue
    from crsw_web.config import Settings
    from promoter import index as index_mod
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import deposit_logic, record

VOCAB = json.loads((Path(__file__).resolve().parent.parent
                    / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
                s3_bucket="crsw", staging_prefix="staging/_test")
READ = dict(SETTINGS, read_s3_access_key="r", read_s3_secret_key="r")
META = {"strand": "rs2", "sensitivity": "green", "state": "0_raw", "project": "csac",
        "dataset": "events", "domain": "quant", "version": "1-0",
        "coverage_start": "2020", "coverage_end": "2021", "subject": ["forced-labour"],
        "abstract": "Events from the CSAC extract, cleaned. " * 6, "creator": "Ada"}
GREEN = "rs2/csac/green/0_raw/events"
AMBER = "rs1/oral/amber/1_interim/interviews"


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class CatalogueBase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.mock = mock_aws(); self.mock.start(); self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")
        self.seed()

    def put_record(self, identifier, meta, entries, depositor, uuid, **extra):
        rec, _, _, _ = deposit_logic.assemble_record(
            meta, None, entries, depositor, "2026-09-01T00:00:00Z", uuid)
        rec.update(extra)
        key = identifier + "/dataset.%s.json" % meta["dataset"]
        self.s3.put_object(Bucket="crsw", Key=key, Body=deposit_logic.record_bytes(rec),
                           Metadata={"depositor": depositor})
        for e in entries:
            self.s3.put_object(Bucket="crsw", Key=identifier + "/" + e["path"],
                               Body=b"x" * e["bytes"])
        return rec

    def seed(self):
        self.green = self.put_record(
            GREEN, META, [record.manifest_entry("events.csv", "a" * 64, 1200),
                          record.manifest_entry("notes/readme.md", "b" * 64, 40)],
            "k1078591", "11111111-1111-4111-8111-111111111111",
            provenance=[{"activity": "clean", "tool": {"name": "clean.py",
                                                       "repo": "https://x.org/r", "commit": "abc1234"}}])
        amber_meta = dict(META, strand="rs1", sensitivity="amber", state="1_interim",
                          project="oral", dataset="interviews", subject=["forced-marriage"],
                          abstract="Interview transcripts, anonymised. " * 6, creator="Bob")
        self.amber = self.put_record(
            AMBER, amber_meta, [record.manifest_entry("t1.txt", "c" * 64, 9)],
            "k2", "22222222-2222-4222-8222-222222222222",
            derived_from=[{"kind": "dataset", "identifier": GREEN,
                           "dataset_uuid": "11111111-1111-4111-8111-111111111111", "version": "1-0"}])
        # The promoter's own index builder is the producer.
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))

    def app(self, **overrides):
        settings = Settings(**dict(READ, **overrides))
        return TestClient(create_app(settings, s3_client=self.s3, vocab_dict=VOCAB,
                                     read_client=s3mod.ReadOnly(self.s3)))


class TestReadRoleOff(CatalogueBase):
    def test_every_route_is_404_and_no_nav_link(self):
        c = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3, vocab_dict=VOCAB))
        for path in ("/datasets", "/datasets.json", "/datasets/" + GREEN,
                     "/datasets/" + GREEN + "/record"):
            self.assertEqual(c.get(path).status_code, 404, path)
        self.assertNotIn("Find data", c.get("/").text)


class TestReadOnlyClient(unittest.TestCase):
    @unittest.skipUnless(HAVE_WEB, "web extras not installed")
    def test_only_reads_are_reachable(self):
        ro = s3mod.ReadOnly(object())
        for name in ("put_object", "delete_object", "copy_object", "create_multipart_upload",
                     "delete_objects", "put_bucket_policy"):
            with self.assertRaises(AttributeError):
                getattr(ro, name)
        with self.assertRaises(AttributeError):
            ro.get_paginator("list_multipart_uploads")
        self.assertEqual(s3mod.ReadOnly.READS, ("get_object", "head_object"))


class TestFind(CatalogueBase):
    def test_lists_search_and_filters(self):
        c = self.app()
        html = c.get("/").text
        self.assertIn('href="/datasets"', html)
        page = c.get("/datasets")
        self.assertEqual(page.status_code, 200)
        self.assertIn("2 datasets", page.text)
        self.assertIn('href="/datasets/%s"' % GREEN, page.text)
        self.assertIn('href="/datasets/%s"' % AMBER, page.text)
        self.assertIn("Index built 2026-", page.text)
        self.assertIn("1.2 KB", page.text)
        page = c.get("/datasets", params={"q": "interview"})
        self.assertIn("1 dataset<", page.text)
        self.assertNotIn(GREEN, page.text)
        page = c.get("/datasets", params={"strand": "rs2"})
        self.assertNotIn(AMBER, page.text)
        page = c.get("/datasets", params={"subject": "forced-marriage"})
        self.assertIn(AMBER, page.text)
        self.assertNotIn(GREEN, page.text)
        page = c.get("/datasets", params={"q": "nothing like this"})
        self.assertIn("Nothing matches", page.text)
        self.assertIn('value="forced-marriage"', page.text)   # subject filter options

    def test_json_feed_for_the_picker(self):
        c = self.app()
        r = c.get("/datasets.json", params={"q": "csac", "limit": 10})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([d["identifier"] for d in body["datasets"]], [GREEN])
        d = body["datasets"][0]
        self.assertEqual(set(d), {"identifier", "dataset_uuid", "dataset", "project", "strand",
                                  "state", "sensitivity", "domain", "depositor", "files",
                                  "bytes", "modified", "subject", "version", "abstract"})
        self.assertLessEqual(len(d["abstract"]), 201)
        self.assertTrue(body["built_at"])
        self.assertEqual(len(c.get("/datasets.json").json()["datasets"]), 2)

    def test_dataset_page_and_record(self):
        c = self.app()
        page = c.get("/datasets/" + GREEN)
        self.assertEqual(page.status_code, 200)
        self.assertIn("events.csv", page.text)
        self.assertIn('href="/datasets/%s/files/notes/readme.md"' % GREEN, page.text)
        self.assertIn("clean.py", page.text)
        self.assertIn("abc1234", page.text)
        self.assertIn('href="/datasets/%s"' % AMBER, page.text)     # derived datasets
        self.assertIn("1.2 KB", page.text)
        r = c.get("/datasets/%s/record" % GREEN)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/json")
        self.assertEqual(json.loads(r.text)["dataset_uuid"], self.green["dataset_uuid"])
        self.assertEqual(c.get("/datasets/rs2/csac/green/0_raw/nope").status_code, 404)
        self.assertEqual(c.get("/datasets/not/a/prefix").status_code, 404)

    def test_amber_page_names_the_steward_and_has_no_links_by_default(self):
        c = self.app()
        page = c.get("/datasets/" + AMBER)
        self.assertEqual(page.status_code, 200)
        self.assertIn("ask the dataset&#39;s steward", page.text)
        self.assertNotIn("/files/t1.txt", page.text)
        self.assertIn('href="/datasets/%s"' % GREEN, page.text)     # derived from
        page = self.app(amber_access="all").get("/datasets/" + AMBER)
        self.assertIn('href="/datasets/%s/files/t1.txt"' % AMBER, page.text)

    def test_index_missing_is_a_notice_not_an_error(self):
        self.s3.delete_object(Bucket="crsw", Key="index/datasets.jsonl")
        c = self.app()
        page = c.get("/datasets")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no index at index/datasets.jsonl yet", page.text)
        # A record is still readable by identifier: the index only finds.
        self.assertEqual(c.get("/datasets/" + GREEN).status_code, 200)


class TestDownload(CatalogueBase):
    """Streamed through the service, resumable, only what the record
    names, only what the amber rule allows."""

    def test_full_download_headers_and_bytes(self):
        c = self.app()
        r = c.get("/datasets/%s/files/events.csv" % GREEN)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, b"x" * 1200)
        self.assertEqual(r.headers["content-length"], "1200")
        self.assertEqual(r.headers["accept-ranges"], "bytes")
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        self.assertIn('filename="events.csv"', r.headers["content-disposition"])
        self.assertIn("filename*=UTF-8''events.csv", r.headers["content-disposition"])
        self.assertTrue(r.headers.get("etag"))
        self.assertTrue(r.headers.get("last-modified"))
        r = c.get("/datasets/%s/files/notes/readme.md" % GREEN)
        self.assertEqual((r.status_code, r.content), (200, b"x" * 40))
        self.assertIn('filename="readme.md"', r.headers["content-disposition"])

    def test_range_resumes_and_a_bad_range_is_416(self):
        c = self.app()
        r = c.get("/datasets/%s/files/events.csv" % GREEN, headers={"Range": "bytes=2-5"})
        self.assertEqual(r.status_code, 206, r.text)
        self.assertEqual(r.content, b"xxxx")
        self.assertEqual(r.headers["content-range"], "bytes 2-5/1200")
        self.assertEqual(r.headers["content-length"], "4")
        r = c.get("/datasets/%s/files/events.csv" % GREEN, headers={"Range": "bytes=5000-6000"})
        self.assertEqual(r.status_code, 416)
        self.assertEqual(r.headers["content-range"], "bytes */1200")

    def test_only_members_the_record_names_are_served(self):
        self.s3.put_object(Bucket="crsw", Key=GREEN + "/extra.bin", Body=b"secret")
        c = self.app()
        self.assertEqual(c.get("/datasets/%s/files/extra.bin" % GREEN).status_code, 404)
        self.assertEqual(c.get("/datasets/%s/files/../events.csv" % GREEN).status_code, 404)
        self.assertEqual(c.get("/datasets/%s/files/dataset.events.json" % GREEN).status_code, 404)
        # Named by the record but gone from the store: says so, not 500.
        self.s3.delete_object(Bucket="crsw", Key=GREEN + "/notes/readme.md")
        r = c.get("/datasets/%s/files/notes/readme.md" % GREEN)
        self.assertEqual(r.status_code, 404)
        self.assertIn("no object", r.json()["detail"])

    def test_amber_by_policy(self):
        url = "/datasets/%s/files/t1.txt" % AMBER
        r = self.app().get(url)
        self.assertEqual(r.status_code, 403)
        self.assertIn("steward", r.json()["detail"])
        self.assertEqual(self.app(amber_access="all").get(url).status_code, 200)
        self.assertEqual(self.app(amber_access="groups",
                                  dev_groups=("er_prj_kdl_slavery",)).get(url).status_code, 403)
        r = self.app(amber_access="groups",
                     dev_groups=("er_prj_kdl_slavery_rs1",)).get(url)
        self.assertEqual((r.status_code, r.content), (200, b"x" * 9))
        # Green is never gated.
        self.assertEqual(self.app(amber_access="groups").get(
            "/datasets/%s/files/events.csv" % GREEN).status_code, 200)

    def test_only_the_read_client_is_used(self):
        staging = mock.MagicMock()
        staging.get_object.side_effect = AssertionError("staging client must not serve downloads")
        c = TestClient(create_app(Settings(**READ), s3_client=staging, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(self.s3)))
        r = c.get("/datasets/%s/files/events.csv" % GREEN)
        self.assertEqual(r.status_code, 200)
        staging.get_object.assert_not_called()
        staging.put_object.assert_not_called()


class TestCatalogueCache(CatalogueBase):
    def test_refreshes_after_the_interval_and_keeps_rows_on_failure(self):
        clock = {"now": 0.0}
        cat = Catalogue(s3mod.ReadOnly(self.s3), "crsw", "index", refresh_seconds=60,
                        clock=lambda: clock["now"])
        self.assertEqual(len(cat.rows()), 2)
        self.put_record("rs3/x/green/2_final/third", dict(META, strand="rs3", project="x",
                                                         state="2_final", dataset="third", creator="Cy"),
                        [record.manifest_entry("a", "d" * 64, 1)], "k3",
                        "33333333-3333-4333-8333-333333333333")
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))
        self.assertEqual(len(cat.rows()), 2)          # not yet
        clock["now"] = 61
        self.assertEqual(len(cat.rows()), 3)
        self.s3.delete_object(Bucket="crsw", Key="index/datasets.jsonl")
        clock["now"] = 122
        self.assertEqual(len(cat.rows()), 3)          # kept
        self.assertIn("no index", cat.error)
        self.assertEqual(cat.get(GREEN)["dataset"], "events")
        self.assertEqual([r["identifier"] for r in cat.search(q="ADA")], [GREEN])


if __name__ == "__main__":
    unittest.main()
