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
    from promoter import embed as embed_mod
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

from crsw_deposit import authority, deposit_logic, places as places_mod, record

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

    def app(self, vocab=None, **overrides):
        settings = Settings(**dict(READ, **overrides))
        return TestClient(create_app(settings, s3_client=self.s3, vocab_dict=vocab or VOCAB,
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
    def test_the_record_route_fetches_the_record_once(self):
        counting = Counting(self.s3)
        c = TestClient(create_app(Settings(**READ), s3_client=self.s3, vocab_dict=VOCAB,
                                  read_client=s3mod.ReadOnly(counting)))
        r = c.get("/datasets/" + GREEN + "/record")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.text)["identifier"], GREEN)
        self.assertEqual(counting.gets[GREEN + "/dataset.events.json"], 1)

    def test_lists_search_and_filters(self):
        c = self.app()
        html = c.get("/").text
        self.assertIn('href="/datasets"', html)
        page = c.get("/datasets")
        self.assertEqual(page.status_code, 200)
        self.assertIn("2 datasets in 2 projects", page.text)
        self.assertIn('href="/datasets/%s"' % GREEN, page.text)
        self.assertIn('href="/datasets/%s"' % AMBER, page.text)
        self.assertIn("Index built 2026-", page.text)
        self.assertIn("1.2 KB", page.text)
        page = c.get("/datasets", params={"q": "interview"})
        self.assertIn("1 dataset in 1 project<", page.text)
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
                                  "bytes", "modified", "subject", "version", "abstract",
                                  "provider", "spatial"})
        self.assertLessEqual(len(d["abstract"]), 201)
        self.assertTrue(body["built_at"])
        self.assertEqual(len(c.get("/datasets.json").json()["datasets"]), 2)

    def test_dataset_page_shows_subject_labels_with_scope_notes(self):
        described = authority.describe_term(
            VOCAB, "forced-labour", scope="Work exacted under the menace of a penalty.")
        page = self.app(vocab=described).get("/datasets/" + GREEN).text
        self.assertIn('<a href="/datasets?subject=forced-labour" '
                      'title="Work exacted under the menace of a penalty.">'
                      'Forced labour <code>forced-labour</code></a>', page)
        # No scope note: no hover text. A flat pre-r9 file: the slug alone.
        page = self.app().get("/datasets/" + GREEN).text
        self.assertIn('<a href="/datasets?subject=forced-labour">'
                      'Forced labour <code>forced-labour</code></a>', page)
        flat = {"vocabulary_version": "x", "domains": VOCAB["domains"],
                "facets": {"practices": ["forced-labour"]}}
        page = self.app(vocab=flat).get("/datasets/" + GREEN).text
        self.assertIn('<a href="/datasets?subject=forced-labour">forced-labour</a>', page)

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


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestGrouping(unittest.TestCase):
    """The find page's list is projects first: `group_projects` folds
    rows into projects in order of first appearance (so newest-first
    and an answer's ranking both survive), `page_of` cuts the projects
    into pages of 10, 25 or 50."""
    def rows(self):
        return [
            {"identifier": "rs2/csac/green/2_final/latam", "strand": "rs2", "project": "csac",
             "files": 3, "bytes": 300, "modified": "2026-10-22T10:00:00Z"},
            {"identifier": "rs1/oral/amber/1_interim/interviews", "strand": "rs1",
             "project": "oral", "files": 1, "bytes": 9, "modified": "2026-10-19T10:00:00Z"},
            {"identifier": "rs2/csac/green/0_raw/events", "strand": "rs2", "project": "csac",
             "files": 2, "bytes": 1240, "modified": "2026-09-01T00:00:00Z"},
        ]

    def test_groups_keep_first_appearance_order_and_sum_the_datasets(self):
        from crsw_web.catalogue import group_projects
        groups = group_projects(self.rows())
        self.assertEqual([(g["strand"], g["project"]) for g in groups],
                         [("rs2", "csac"), ("rs1", "oral")])
        csac = groups[0]
        self.assertEqual([r["identifier"].split("/")[-1] for r in csac["rows"]],
                         ["latam", "events"])
        self.assertEqual((csac["files"], csac["bytes"], csac["modified"]),
                         (5, 1540, "2026-10-22T10:00:00Z"))
        self.assertEqual(group_projects([]), [])
        # A project name reused in another strand is another project.
        rows = self.rows() + [{"identifier": "rs3/csac/green/0_raw/x", "strand": "rs3",
                               "project": "csac", "files": 0, "bytes": 0, "modified": ""}]
        self.assertEqual(len(group_projects(rows)), 3)

    def test_pages_clamp_the_page_and_the_size(self):
        from crsw_web.catalogue import page_of, PER_CHOICES, PER_DEFAULT
        self.assertEqual((PER_CHOICES, PER_DEFAULT), ((10, 25, 50), 10))
        items = list(range(23))
        self.assertEqual(page_of(items, 1, 10), (items[:10], 1, 3, 10))
        self.assertEqual(page_of(items, 3, 10), (items[20:], 3, 3, 10))
        self.assertEqual(page_of(items, 9, 10), (items[20:], 3, 3, 10))      # past the end
        self.assertEqual(page_of(items, 0, 10), (items[:10], 1, 3, 10))      # before it
        self.assertEqual(page_of(items, 1, 25), (items, 1, 1, 25))
        self.assertEqual(page_of(items, 2, 50), (items, 1, 1, 50))
        self.assertEqual(page_of(items, 1, 7), (items[:10], 1, 3, 10))       # not a choice
        self.assertEqual(page_of(items, 1, None), (items[:10], 1, 3, 10))
        self.assertEqual(page_of([], 1, 10), ([], 1, 1, 10))


class TestProjectAccordion(CatalogueBase):
    """The find page groups datasets under their project, closed while
    browsing and open once a search narrowed the list, ten projects a
    page with the page size in the address and remembered."""
    def seed(self):
        super().seed()
        for n in range(11):
            ident = "rs3/proj-%02d/green/0_raw/d" % n
            self.put_record(ident, dict(META, strand="rs3", project="proj-%02d" % n, dataset="d",
                                        abstract="Project number %d. " % n * 10),
                            [record.manifest_entry("d.csv", ("%x" % n) * 64, 10)],
                            "k1078591", "%08d-0000-4000-8000-000000000000" % n,
                            modified="2026-08-%02dT00:00:00Z" % (n + 1))
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))

    def test_projects_are_closed_when_browsing_and_open_when_searching(self):
        c = self.app()
        html = c.get("/datasets").text
        self.assertIn("13 datasets in 13 projects", html)
        self.assertIn('<details class="project">', html)
        self.assertNotIn('<details class="project" open>', html)
        # Project, strand, then the datasets inside; "Where" has gone.
        self.assertIn('<span class="name">csac</span><span class="tag strand">rs2</span>', html)
        self.assertLess(html.index('<span class="name">csac</span>'),
                        html.index('href="/datasets/%s"' % GREEN))
        self.assertNotIn("<th>Where</th>", html)
        self.assertIn("<th>State</th>", html)
        self.assertIn("1.2 KB", html)
        for params in ({"q": "interview"}, {"strand": "rs1"}, {"sensitivity": "amber"}):
            html = c.get("/datasets", params=params).text
            self.assertIn('<details class="project" open>', html, params)
            self.assertNotIn('<details class="project">', html, params)
            self.assertIn("1 dataset in 1 project", html)

    def test_ten_projects_a_page_newest_first(self):
        c = self.app()
        html = c.get("/datasets").text
        self.assertEqual(html.count('<details class="project"'), 10)
        self.assertIn("Projects 1–10 of 13", html)
        self.assertIn('href="/datasets?page=2&amp;per=10">Next', html)
        self.assertNotIn("Previous</a>", html)
        # Newest project first: the two seeded datasets were modified in
        # September, the eleven others in August.
        for name in ("csac", "oral"):
            self.assertLess(html.index('<span class="name">%s</span>' % name),
                            html.index('<span class="name">proj-10</span>'))
        self.assertNotIn('<span class="name">proj-02</span>', html)
        html = c.get("/datasets", params={"page": 2}).text
        self.assertEqual(html.count('<details class="project"'), 3)
        self.assertIn("Projects 11–13 of 13", html)
        self.assertIn('<span class="name">proj-02</span>', html)
        self.assertIn('href="/datasets?page=1&amp;per=10">Previous', html)
        self.assertNotIn("Next</a>", html)
        # The pager carries the filters along.
        html = c.get("/datasets", params={"strand": "rs3", "page": 2}).text
        self.assertIn("11 datasets in 11 projects", html)
        self.assertIn('href="/datasets?strand=rs3&amp;page=1&amp;per=10">Previous', html)
        # Past the end lands on the last page, not an empty one.
        html = c.get("/datasets", params={"page": 9}).text
        self.assertIn("Projects 11–13 of 13", html)

    def test_page_size_in_the_address_and_remembered(self):
        c = self.app()
        r = c.get("/datasets", params={"per": 25})
        self.assertEqual(r.text.count('<details class="project"'), 13)
        self.assertNotIn("Projects 1–", r.text)                    # one page: no pager
        self.assertIn('href="/datasets?page=1&amp;per=10">10</a>', r.text)   # but the sizes
        self.assertIn('<span class="current">25</span>', r.text)
        self.assertEqual(r.cookies.get("crsw_per"), "25")
        # The browser sends the cookie back: the choice sticks.
        self.assertEqual(c.get("/datasets").text.count('<details class="project"'), 13)
        c.cookies.set("crsw_per", "7")                                  # not a choice
        self.assertEqual(c.get("/datasets").text.count('<details class="project"'), 10)
        r = c.get("/datasets", params={"per": 7})
        self.assertEqual(r.text.count('<details class="project"'), 10)
        self.assertIsNone(r.cookies.get("crsw_per"))
        # Fewer projects than a page: no pager at all.
        self.assertNotIn('class="pager"', c.get("/datasets", params={"strand": "rs2"}).text)


class TestHierarchy(CatalogueBase):
    """Vocabulary handoff, part 1: a subject filter follows the hierarchy,
    a retired slug finds its successors, and the select shows the tree."""
    WARS = "rs2/csac/green/2_final/wars"
    DISP = "rs2/csac/green/2_final/displaced"

    def seed(self):
        super().seed()
        v = authority.apply_change(VOCAB, {
            "kind": "add", "to": ["interstate-war"], "facet": "contexts",
            "label": "Interstate war", "broader": "armed-conflict",
            "date": "2026-10-01", "by": "k1"})
        self.vocab = authority.apply_change(v, {
            "kind": "rename", "from": ["forced-displacement"], "to": ["displacement"],
            "date": "2026-10-02", "by": "k1"})
        self.put_record(self.WARS, dict(META, state="2_final", dataset="wars",
                                        subject=["interstate-war"]),
                        [record.manifest_entry("wars.csv", "e" * 64, 10)],
                        "k1078591", "44444444-4444-4444-8444-444444444444")
        self.put_record(self.DISP, dict(META, state="2_final", dataset="displaced",
                                        subject=["displacement"]),
                        [record.manifest_entry("d.csv", "f" * 64, 10)],
                        "k1078591", "55555555-5555-4555-8555-555555555555")
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))

    def ids(self, client, **params):
        return [d["identifier"]
                for d in client.get("/datasets.json", params=params).json()["datasets"]]

    def test_a_parent_finds_a_dataset_tagged_only_with_its_child(self):
        c = self.app(vocab=self.vocab)
        page = c.get("/datasets", params={"subject": "armed-conflict"})
        self.assertIn('href="/datasets/%s"' % self.WARS, page.text)
        self.assertNotIn(GREEN, page.text)
        self.assertIn("1 dataset in 1 project with subject armed-conflict (and 1 narrower term)", page.text)
        self.assertNotIn(self.WARS, c.get("/datasets", params={"subject": "occupation"}).text)
        self.assertEqual(self.ids(c, subject="interstate-war"), [self.WARS])

    def test_the_json_feed_agrees_and_says_what_it_matched(self):
        c = self.app(vocab=self.vocab)
        body = c.get("/datasets.json", params={"subject": "armed-conflict"}).json()
        self.assertEqual([d["identifier"] for d in body["datasets"]], [self.WARS])
        self.assertEqual(body["subject_filter"],
                         {"asked": "armed-conflict",
                          "matched": ["armed-conflict", "interstate-war"], "how": "narrower"})
        self.assertNotIn("subject_filter", c.get("/datasets.json").json())

    def test_a_retired_slug_finds_its_successor_with_a_notice(self):
        c = self.app(vocab=self.vocab)
        page = c.get("/datasets", params={"subject": "forced-displacement"})
        self.assertIn('href="/datasets/%s"' % self.DISP, page.text)
        self.assertIn("forced-displacement is no longer a current term; showing displacement.",
                      page.text)
        self.assertIn('<option value="displacement" selected>', page.text)
        body = c.get("/datasets.json", params={"subject": "forced-displacement"}).json()
        self.assertEqual([d["identifier"] for d in body["datasets"]], [self.DISP])
        self.assertEqual(body["subject_filter"]["how"], "replaced")
        self.assertIn("showing displacement", body["notice"])

    def test_a_retired_slug_with_no_successor_matches_nothing_and_says_so(self):
        v = authority.apply_change(self.vocab, {"kind": "retire", "from": ["maritime"],
                                                "date": "2026-10-03", "by": "k1"})
        page = self.app(vocab=v).get("/datasets", params={"subject": "maritime"})
        self.assertIn("maritime was retired with no successor; nothing carries it now.",
                      page.text)
        self.assertIn("0 datasets", page.text)

    def test_the_select_is_grouped_by_facet_and_shows_the_tree(self):
        html = self.app(vocab=self.vocab).get("/datasets").text
        self.assertIn('<optgroup label="contexts">', html)
        # The parent is offered although no dataset carries it directly.
        self.assertIn('<option value="armed-conflict">Armed conflict (armed-conflict)</option>',
                      html)
        self.assertIn('<option value="interstate-war">\u2007\u2007Interstate war '
                      '(interstate-war)</option>', html)
        # The whole vocabulary is offered, a term nobody has used yet too.
        self.assertIn('<option value="occupation">Occupation (occupation)</option>', html)
        self.assertNotIn('value="forced-displacement"', html)   # retired
        self.assertIn('<option value="displacement">', html)

    def test_a_term_the_vocabulary_does_not_know_is_still_offered(self):
        self.put_record("rs2/csac/green/2_final/old", dict(META, state="2_final", dataset="old",
                                                           subject=["zzz-old"]),
                        [record.manifest_entry("o.csv", "9" * 64, 10)],
                        "k1078591", "66666666-6666-4666-8666-666666666666")
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))
        html = self.app(vocab=self.vocab).get("/datasets").text
        self.assertIn('<option value="zzz-old">zzz-old (zzz-old)</option>', html)

    def test_the_tree_is_built_once_per_vocabulary(self):
        docs = {"doc": self.vocab}
        cat = Catalogue(s3mod.ReadOnly(self.s3), "crsw", "index",
                        vocabulary=lambda: docs["doc"])
        first = cat.term_tree()
        self.assertIn("occupation", [t["slug"] for t in first])
        self.assertIs(cat.term_tree(), first)
        docs["doc"] = authority.apply_change(self.vocab, {
            "kind": "add", "to": ["civil-unrest"], "facet": "contexts",
            "label": "Civil unrest", "date": "2026-10-04", "by": "k1"})
        again = cat.term_tree()
        self.assertIsNot(again, first)
        self.assertIn("civil-unrest", [t["slug"] for t in again])

    # A1: the count line names only the narrower terms the results carry.
    def test_the_count_line_counts_only_narrower_terms_the_results_carry(self):
        v = authority.apply_change(self.vocab, {
            "kind": "add", "to": ["civil-war"], "facet": "contexts",
            "label": "Civil war", "broader": "armed-conflict",
            "date": "2026-10-04", "by": "k1"})
        c = self.app(vocab=v)
        page = c.get("/datasets", params={"subject": "armed-conflict"})
        self.assertIn("1 dataset in 1 project with subject armed-conflict (and 1 narrower term)", page.text)
        self.assertNotIn("2 narrower", page.text)
        body = c.get("/datasets.json", params={"subject": "armed-conflict"}).json()
        self.assertEqual(body["subject_filter"]["matched"],
                         ["armed-conflict", "interstate-war", "civil-war"])

    def test_no_narrower_phrase_when_no_result_carries_a_narrower_term(self):
        v = authority.apply_change(self.vocab, {
            "kind": "add", "to": ["bonded-labour"], "facet": "practices",
            "label": "Bonded labour", "broader": "forced-labour",
            "date": "2026-10-04", "by": "k1"})
        c = self.app(vocab=v)
        page = c.get("/datasets", params={"subject": "forced-labour"})
        self.assertIn('href="/datasets/%s"' % GREEN, page.text)
        self.assertNotIn("narrower", page.text)
        body = c.get("/datasets.json", params={"subject": "forced-labour"}).json()
        self.assertEqual(body["subject_filter"],
                         {"asked": "forced-labour", "matched": ["forced-labour", "bonded-labour"],
                          "how": "narrower"})

    def test_a_flat_vocabulary_is_a_plain_list(self):
        html = self.app().get("/datasets").text
        self.assertIn('<option value="forced-labour">Forced labour (forced-labour)</option>',
                      html)
        # The subject select is flat; the place select beside it is a tree.
        subject_select = html[html.index('<select id="subject"'):html.index('<label for="place">')]
        self.assertNotIn("\u2007", subject_select)


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


class Counting:
    """An S3 client that counts get_object calls per key and keeps the
    arguments, passing everything through."""
    def __init__(self, client):
        self._client = client
        self.gets = {}
        self.calls = []

    def get_object(self, **kw):
        self.gets[kw["Key"]] = self.gets.get(kw["Key"], 0) + 1
        self.calls.append(kw)
        return self._client.get_object(**kw)

    def __getattr__(self, name):
        return getattr(self._client, name)


class Toy:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class TestCatalogueCache(CatalogueBase):
    def test_unchanged_files_are_not_downloaded_or_parsed_again(self):
        embed_mod.refresh(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"),
                          Toy(), "toy", {"green"})
        counting = Counting(self.s3)
        clock = {"now": 0.0}
        cat = Catalogue(s3mod.ReadOnly(counting), "crsw", "index", refresh_seconds=60,
                        clock=lambda: clock["now"])
        self.assertEqual(len(cat.rows()), 2)
        self.assertTrue(cat.has_vectors())
        rows_obj, vec_obj = cat._rows, cat._vectors
        self.assertEqual(counting.gets, {"index/datasets.jsonl": 1, "index/embeddings.parquet": 1})
        # The interval passes, nothing changed: asked with the ETags, told
        # "not modified", nothing parsed.
        clock["now"] = 61
        self.assertEqual(len(cat.rows()), 2)
        self.assertEqual(counting.gets, {"index/datasets.jsonl": 2, "index/embeddings.parquet": 2})
        self.assertTrue(all(kw.get("IfNoneMatch") for kw in counting.calls[2:]))
        self.assertIs(cat._rows, rows_obj)
        self.assertIs(cat._vectors, vec_obj)
        self.assertIsNone(cat.error)
        # The index changes, the embeddings do not: only the index is re-read.
        self.put_record("rs3/x/green/2_final/third", dict(META, strand="rs3", project="x",
                                                         state="2_final", dataset="third", creator="Cy"),
                        [record.manifest_entry("a", "d" * 64, 1)], "k3",
                        "33333333-3333-4333-8333-333333333333")
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))
        clock["now"] = 122
        self.assertEqual(len(cat.rows()), 3)
        self.assertIsNot(cat._rows, rows_obj)
        self.assertIs(cat._vectors, vec_obj)

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


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestPlaces(CatalogueBase):
    """r11 §4: a place filter follows the M49 hierarchy both ways, the
    select shows the tree, and the dataset page names provider and
    places."""
    LATAM = "rs2/csac/green/2_final/latam"
    COLOMBIA = "rs2/csac/green/2_final/colombia"
    THAI = "rs2/csac/green/2_final/thai"

    def seed(self):
        super().seed()
        self.put_record(self.LATAM, dict(META, state="2_final", dataset="latam"),
                        [record.manifest_entry("l.csv", "1" * 64, 10)],
                        "k1078591", "77777777-7777-4777-8777-777777777777",
                        provider=["acled"], spatial=["m49:419"],
                        spatial_note="50 countries, admin1 level", notes="Weekly aggregates.")
        self.put_record(self.COLOMBIA, dict(META, state="2_final", dataset="colombia"),
                        [record.manifest_entry("c.csv", "2" * 64, 10)],
                        "k1078591", "88888888-8888-4888-8888-888888888888",
                        spatial=["iso:CO"])
        self.put_record(self.THAI, dict(META, state="2_final", dataset="thai"),
                        [record.manifest_entry("t.csv", "3" * 64, 10)],
                        "k1078591", "99999999-9999-4999-8999-999999999999",
                        spatial=["iso:TH", "iso:AU"])
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))

    def ids(self, client, **params):
        return sorted(d["identifier"]
                      for d in client.get("/datasets.json", params=params).json()["datasets"])

    def test_a_country_finds_a_dataset_tagged_with_its_subregion(self):
        c = self.app()
        self.assertEqual(self.ids(c, place="iso:CO"), [self.COLOMBIA, self.LATAM])
        page = c.get("/datasets", params={"place": "iso:CO"})
        self.assertIn("2 datasets in 1 project covering Colombia (or a place containing or inside it)",
                      page.text)
        self.assertIn('<option value="iso:CO" selected>', page.text)

    def test_a_region_finds_a_dataset_tagged_with_a_country_inside_it(self):
        c = self.app()
        self.assertEqual(self.ids(c, place="m49:019"), [self.COLOMBIA, self.LATAM])
        self.assertEqual(self.ids(c, place="m49:001"), [self.COLOMBIA, self.LATAM, self.THAI])
        self.assertEqual(self.ids(c, place="m49:035"), [self.THAI])
        self.assertEqual(self.ids(c, place="iso:MX"), [self.LATAM])

    def test_the_json_feed_says_what_the_place_matched(self):
        c = self.app()
        body = c.get("/datasets.json", params={"place": "iso:CO"}).json()
        self.assertEqual(body["place_filter"]["asked"], "iso:CO")
        self.assertEqual(body["place_filter"]["how"], "hierarchy")
        self.assertEqual(body["place_filter"]["matched"][:5],
                         ["m49:001", "m49:019", "m49:419", "m49:005", "iso:CO"])
        self.assertEqual(body["datasets"][0]["spatial"], body["datasets"][0]["spatial"])
        self.assertNotIn("place_filter", c.get("/datasets.json").json())
        rows = {d["identifier"]: d for d in c.get("/datasets.json").json()["datasets"]}
        self.assertEqual((rows[self.LATAM]["provider"], rows[self.LATAM]["spatial"]),
                         (["acled"], ["m49:419"]))

    def test_the_place_select_is_grouped_by_region_and_indented(self):
        html = self.app().get("/datasets").text
        self.assertIn('<optgroup label="World">', html)
        self.assertIn('<optgroup label="Americas">', html)
        self.assertIn('<option value="m49:419">  Latin America and the Caribbean '
                      '(m49:419)</option>', html)
        self.assertIn('<option value="iso:CO">      Colombia '
                      '(iso:CO)</option>', html)

    def test_no_places_document_matches_as_typed(self):
        cat = Catalogue(s3mod.ReadOnly(self.s3), "crsw", "index")
        self.assertEqual([r["identifier"] for r in cat.search(place="iso:CO")],
                         [self.COLOMBIA])
        self.assertEqual(cat.expand_place("iso:CO"), (["iso:CO"], "unknown"))
        self.assertEqual([p["code"] for p in cat.place_options()],
                         ["iso:AU", "iso:CO", "iso:TH", "m49:419"])

    def test_the_place_options_are_built_once_per_document(self):
        docs = {"doc": json.loads(places_mod.bundled_places_path().read_text(encoding="utf-8"))}
        cat = Catalogue(s3mod.ReadOnly(self.s3), "crsw", "index",
                        places=lambda: docs["doc"])
        with mock.patch("crsw_web.catalogue.places_mod.tree",
                        wraps=places_mod.tree) as tree:
            cat.place_options(); cat.place_options()
            self.assertEqual(tree.call_count, 1)
            docs["doc"] = dict(docs["doc"])
            cat.place_options()
            self.assertEqual(tree.call_count, 2)

    def test_the_dataset_page_shows_provider_and_places_by_label(self):
        page = self.app().get("/datasets/" + self.LATAM).text
        self.assertIn('<a href="https://acleddata.com/" rel="noopener">Armed Conflict Location '
                      '&amp; Event Data (ACLED)</a> <code>acled</code>', page)
        self.assertIn('<a href="/datasets?place=m49:419">Latin America and the Caribbean</a> '
                      '<code>m49:419</code>', page)
        self.assertIn("<dt>Place note</dt><dd>50 countries, admin1 level</dd>", page)
        self.assertIn("<dt>Notes</dt><dd>Weekly aggregates.</dd>", page)
        self.assertIn("<dt>Domain</dt><dd>quant", page)
