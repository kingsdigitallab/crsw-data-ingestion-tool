"""Depositing to a dataset that already exists, through the web: the
lookup the form uses as the researcher types, the confirmation a
create needs, no silent version drop, and the finalise counts. All of
it through the read role; with the read role off the API is as it was."""
import hashlib
import json
import unittest

from tests.test_web_catalogue import (HAVE_WEB, CatalogueBase, META, GREEN, AMBER,
                                      READ, SETTINGS, VOCAB)

if HAVE_WEB:
    from fastapi.testclient import TestClient
    from crsw_web import s3 as s3mod
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    from crsw_web.deposits import DepositStore
    from crsw_deposit import record
    from promoter import index as index_mod

FORM = dict(META, source_type="archive", source_detail="The CSAC database extract")
LOOKUP = {"strand": "rs2", "project": "csac", "sensitivity": "green", "state": "0_raw",
          "dataset": "events"}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class ExistingBase(CatalogueBase):
    def lookup(self, c, **params):
        r = c.get("/deposits/lookup", params=dict(LOOKUP, **params))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestLookup(ExistingBase):
    def test_off_with_the_read_role(self):
        c = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3, vocab_dict=VOCAB))
        self.assertEqual(c.get("/deposits/lookup", params=LOOKUP).status_code, 404)

    def test_an_existing_dataset_is_described_with_its_defaults_and_manifest(self):
        body = self.lookup(self.app())
        self.assertTrue(body["exists"])
        self.assertEqual(body["identifier"], GREEN)
        self.assertEqual((body["summary"]["files"], body["summary"]["version"],
                          body["summary"]["dataset_uuid"]),
                         (2, "1-0", "11111111-1111-4111-8111-111111111111"))
        d = body["defaults"]
        self.assertEqual((d["version"], d["creator"], d["subject"], d["domain"],
                          d["coverage_start"], d["coverage_end"]),
                         ("1-0", "Ada", ["forced-labour"], "quant", "2020", "2021"))
        self.assertEqual(d["abstract"], META["abstract"])     # the whole text, not the index's cut
        self.assertNotIn("derived_from", d)
        self.assertEqual([(m["path"], m["bytes"]) for m in body["manifest"]],
                         [("events.csv", 1200), ("notes/readme.md", 40)])
        self.assertEqual((body["similar"], body["elsewhere"], body["pending"]),
                         ({"project": [], "dataset": []}, [], []))

    def test_a_new_name_with_near_matches_and_the_same_name_elsewhere(self):
        c = self.app()
        body = self.lookup(c, dataset="event")
        self.assertFalse(body["exists"])
        self.assertIsNone(body["summary"])
        self.assertEqual(body["similar"]["dataset"], ["events"])
        body = self.lookup(c, project="csacs", dataset="new-thing")
        self.assertEqual(body["similar"], {"project": ["csac"], "dataset": []})
        # Same name in the project, but under another sensitivity or state.
        body = self.lookup(c, sensitivity="amber")
        self.assertFalse(body["exists"])
        self.assertEqual([r["identifier"] for r in body["elsewhere"]], [GREEN])
        body = self.lookup(c, state="2_final")
        self.assertEqual([r["identifier"] for r in body["elsewhere"]], [GREEN])

    def test_a_deposit_still_in_staging_counts_as_pending(self):
        c = self.app()
        d = c.post("/deposits", json=dict(FORM, dataset="brand-new")).json()
        store = DepositStore(self.s3, "crsw", "staging/_test")
        body = self.lookup(c, dataset="brand-new")
        self.assertFalse(body["exists"])
        self.assertEqual([(p["id"], p["user"]) for p in body["pending"]], [(d["id"], "k1078591")])
        # Finalised deposits still waiting for the promoter count too; a
        # deposit of another user is shown by its user, as in the audit.
        dep = store.load("k1078591", d["id"])
        dep.user = "k2"
        store.save(dep)
        self.s3.delete_object(Bucket="crsw", Key=store.control_key("k1078591", d["id"]))
        body = self.lookup(c, dataset="brand-new")
        self.assertEqual([p["user"] for p in body["pending"]], ["k2"])


class TestCreateOnExisting(ExistingBase):
    def test_needs_confirmation_and_records_what_it_merges_with(self):
        c = self.app()
        r = c.post("/deposits", json=FORM)
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["identifier"], GREEN)
        self.assertEqual(detail["summary"]["files"], 2)
        self.assertIn("exists", detail["message"])
        r = c.post("/deposits", json=dict(FORM, confirm_existing=True))
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["merges_with"], GREEN)
        dep = DepositStore(self.s3, "crsw", "staging/_test").load("k1078591", body["id"])
        self.assertEqual((dep.merges_with, dep.existing_uuid),
                         (GREEN, "11111111-1111-4111-8111-111111111111"))
        # The staged record keeps its own fresh UUID: the promoter keeps
        # the destination's, as it always has.
        self.assertNotEqual(dep.dataset_uuid, dep.existing_uuid)

    def test_a_pending_deposit_also_needs_confirmation(self):
        c = self.app()
        c.post("/deposits", json=dict(FORM, dataset="brand-new"))
        r = c.post("/deposits", json=dict(FORM, dataset="brand-new"))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("waiting", r.json()["detail"]["message"])
        r = c.post("/deposits", json=dict(FORM, dataset="brand-new", confirm_existing=True))
        self.assertEqual(r.status_code, 201, r.text)

    def test_a_lower_version_is_refused_with_a_plain_message(self):
        c = self.app()
        r = c.post("/deposits", json=dict(FORM, version="0-9", confirm_existing=True))
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("lower than the dataset's 1-0", r.json()["detail"]["errors"]["version"])
        r = c.post("/deposits", json=dict(FORM, version="2-0", confirm_existing=True))
        self.assertEqual(r.status_code, 201, r.text)

    def test_a_new_dataset_and_the_read_role_off_need_nothing(self):
        c = self.app()
        r = c.post("/deposits", json=dict(FORM, dataset="brand-new"))
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIsNone(r.json()["merges_with"])
        off = TestClient(create_app(Settings(**SETTINGS), s3_client=self.s3, vocab_dict=VOCAB))
        r = off.post("/deposits", json=FORM)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertNotIn("merges_with", r.json())


class TestFinaliseCounts(ExistingBase):
    def test_added_updated_unchanged_against_the_live_record(self):
        same = b"these bytes are already in the store\n"
        self.put_record("rs2/csac/green/0_raw/counted", dict(META, dataset="counted"),
                        [record.manifest_entry("same.txt", hashlib.sha256(same).hexdigest(),
                                               len(same)),
                         record.manifest_entry("changed.txt", "d" * 64, 5),
                         record.manifest_entry("kept.txt", "e" * 64, 5)],
                        "k1078591", "33333333-3333-4333-8333-333333333333")
        index_mod.write(self.s3, "crsw", "index", index_mod.rows(self.s3, "crsw"))
        c = self.app()
        d = c.post("/deposits", json=dict(FORM, dataset="counted", confirm_existing=True)).json()
        for member, data in (("same.txt", same), ("changed.txt", b"new words"),
                             ("added.txt", b"brand new")):
            r = c.put("/deposits/%s/files/%s" % (d["id"], member), content=data)
            self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/deposits/%s/finalise" % d["id"])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["added"], body["updated"], body["unchanged"]),
                         (["added.txt"], ["changed.txt"], ["same.txt"]))
        self.assertEqual(body["merges_with"], "rs2/csac/green/0_raw/counted")
        self.assertEqual(body["files"], 3)        # this deposit's; the union is the promoter's

    def test_a_first_deposit_counts_everything_as_added(self):
        c = self.app()
        d = c.post("/deposits", json=dict(FORM, dataset="brand-new")).json()
        c.put("/deposits/%s/files/a.txt" % d["id"], content=b"a")
        body = c.post("/deposits/%s/finalise" % d["id"]).json()
        self.assertEqual((body["added"], body["updated"], body["unchanged"]), (["a.txt"], [], []))
