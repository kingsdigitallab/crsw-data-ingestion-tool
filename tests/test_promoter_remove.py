"""`promoter remove` (r10): a dataset leaves the store only through the
promoter. Every object version under its prefix goes, members first and
the record last, then the derived layers are rebuilt; a dry run unless
`--confirm` repeats every identifier. The store is moto with versioning
on, as the real bucket is."""
import io
import json
import os
import unittest
from unittest import mock

from tests.test_promoter import HAVE_WEB, PromoterBase, FORM, VOCAB

if HAVE_WEB:
    import pyarrow.parquet as pq
    from crsw_deposit import deposit_logic, record
    from crsw_web import llm
    from promoter import __main__ as cli

ENV = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
       "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
       "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": "",
       "PROMOTER_AUDIT_PREFIX": ""}
LLM_ENV = {"PROMOTER_LLM_BASE_URL": "https://ai.example/api/v1",
           "PROMOTER_LLM_API_KEY": "sk-test"}
A = "rs2/csac/green/0_raw/promo"
B = "rs2/csac/green/0_raw/promo-b"       # A's name is a prefix of B's: B must stay
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
CREATED = "2026-08-25T10:00:00Z"


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class RemoveBase(PromoterBase):
    def setUp(self):
        super().setUp()
        self.s3.put_bucket_versioning(Bucket="crsw",
                                      VersioningConfiguration={"Status": "Enabled"})

        def fake_embed(platform, texts, batch=32):
            return [[float(len(t)), 0.5, -1.0] for t in texts]
        p = mock.patch.object(llm.Platform, "embed", fake_embed)
        p.start(); self.addCleanup(p.stop)

        def fake_fingerprint(platform):
            return platform.embed_model + "|Fake/Model|FP16|1"
        fp = mock.patch.object(llm.Platform, "embed_fingerprint", fake_fingerprint)
        fp.start(); self.addCleanup(fp.stop)

    def place(self, dataset, dataset_uuid, derived_from=None, identifier=None):
        """A dataset in place under rs2/csac/green/0_raw/: one.csv written
        twice (a noncurrent version), two.txt once, gone.txt written then
        deleted (a delete marker), and the record. Returns (prefix, the
        record's bytes)."""
        prefix = "rs2/csac/green/0_raw/" + dataset
        meta = dict(FORM, dataset=dataset)
        if derived_from:
            meta["derived_from"] = [{"kind": "dataset", "identifier": derived_from}]
        entries = [record.manifest_entry("one.csv", "a" * 64, 4),
                   record.manifest_entry("two.txt", "b" * 64, 5)]
        rec, _, _, _ = deposit_logic.assemble_record(meta, None, entries, "alice",
                                                     CREATED, dataset_uuid)
        if identifier:
            rec["identifier"] = identifier
        body = deposit_logic.record_bytes(rec)

        def put(key, data):
            self.s3.put_object(Bucket="crsw", Key=key, Body=data,
                               Metadata={"depositor": "alice"})
        put(prefix + "/one.csv", b"a,b\n")
        put(prefix + "/one.csv", b"c,d\n")
        put(prefix + "/two.txt", b"hello")
        put(prefix + "/gone.txt", b"x")
        self.s3.delete_object(Bucket="crsw", Key=prefix + "/gone.txt")
        put(prefix + "/dataset.%s.json" % dataset, body)
        return prefix, body

    def cli(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(ENV, **(env or {}))), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary",
                        return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(argv + ["--env-file", os.devnull])
        lines = []
        for c in printed.call_args_list:
            if not c.args:
                continue
            try:
                lines.append(json.loads(c.args[0]))
            except ValueError:
                lines.append({"action": "_text", "text": c.args[0]})
        return code, lines

    def versions(self, prefix):
        r = self.s3.list_object_versions(Bucket="crsw", Prefix=prefix + "/")
        return sorted((v["Key"], v["VersionId"])
                      for v in r.get("Versions", []) + r.get("DeleteMarkers", []))

    @staticmethod
    def only(lines, action):
        return [l for l in lines if l["action"] == action]

    def record_calls(self):
        """Patch delete_objects so each call's keys are recorded (and the
        call still happens)."""
        calls = []
        orig = self.s3.delete_objects

        def spy(**kw):
            calls.append([o["Key"] for o in kw["Delete"]["Objects"]])
            return orig(**kw)
        p = mock.patch.object(self.s3, "delete_objects", side_effect=spy)
        p.start(); self.addCleanup(p.stop)
        return calls


class TestRefusals(RemoveBase):
    def test_a_malformed_identifier_is_refused_before_any_request(self):
        with mock.patch.object(self.s3, "get_object", side_effect=AssertionError("asked")), \
             mock.patch.object(self.s3, "list_object_versions",
                               side_effect=AssertionError("listed")):
            code, lines = self.cli(["remove", "--dataset", "rs9/x", "--confirm", "rs9/x"])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertEqual(refused["prefix"], "rs9/x")
        self.assertIn("not a dataset identifier", refused["problems"][0])

    def test_a_prefix_with_no_record_is_refused(self):
        c = "rs2/csac/green/0_raw/loose"
        self.s3.put_object(Bucket="crsw", Key=c + "/one.csv", Body=b"x")
        before = self.versions(c)
        code, lines = self.cli(["remove", "--dataset", c, "--confirm", c])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertIn("no record in place", refused["problems"][0])
        self.assertEqual(self.versions(c), before)
        self.assertEqual(self.only(lines, "dataset_removed"), [])

    def test_a_record_that_names_another_prefix_is_refused(self):
        prefix, _ = self.place("promo", UUID_A, identifier="rs2/csac/green/0_raw/other")
        before = self.versions(prefix)
        code, lines = self.cli(["remove", "--dataset", prefix, "--confirm", prefix])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertIn("does not match its location", refused["problems"][0])
        self.assertEqual(self.versions(prefix), before)

    def test_without_confirm_nothing_is_deleted(self):
        prefix, _ = self.place("promo", UUID_A)
        before = self.versions(prefix)
        code, lines = self.cli(["remove", "--dataset", prefix])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.only(lines, "would_remove")), 1)
        self.assertEqual(self.only(lines, "dataset_removed"), [])
        fin = self.only(lines, "finish")[-1]
        self.assertTrue(fin["dry_run"] and fin["nothing_done"])
        self.assertEqual(self.versions(prefix), before)

    def test_confirm_must_repeat_every_identifier_exactly(self):
        self.place("promo", UUID_A)
        self.place("promo-b", UUID_B)
        before = self.versions(A) + self.versions(B)
        for argv in (["--dataset", A, "--dataset", B, "--confirm", A],
                     ["--dataset", A, "--confirm", B],
                     ["--dataset", A, "--confirm", A, "--confirm", B]):
            code, lines = self.cli(["remove"] + argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(self.only(lines, "start"), [], argv)
            self.assertIn("--confirm must repeat every --dataset identifier",
                          lines[-1].get("text", ""))
        self.assertEqual(self.versions(A) + self.versions(B), before)

    def test_no_wildcard_of_any_kind(self):
        for argv in (["remove"], ["remove", "--strand", "rs2"], ["remove", "--all"],
                     ["remove", "--dataset", A, "--staging", "x"]):
            with self.assertRaises(SystemExit, msg=argv), mock.patch("builtins.print"), \
                 mock.patch("sys.stderr"):
                cli.main(argv + ["--env-file", os.devnull])

    def test_the_authorised_policy_applies_in_reverse(self):
        prefix, _ = self.place("promo", UUID_A)
        before = self.versions(prefix)
        code, lines = self.cli(["remove", "--dataset", prefix, "--confirm", prefix,
                                "--by", "k1078591"],
                               env={"PROMOTER_AUTHORISED": '{"k1078591": ["rs1"]}'})
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertIn("PROMOTER_AUTHORISED", refused["problems"][0])
        self.assertEqual(self.versions(prefix), before)


class TestDryRun(RemoveBase):
    def test_the_plan_counts_every_version_and_names_what_remains(self):
        _, body = self.place("promo", UUID_A)
        self.place("promo-b", UUID_B, derived_from=A)
        before = self.versions(A) + self.versions(B)
        code, lines = self.cli(["remove", "--dataset", A])
        self.assertEqual(code, 0, lines)
        start = lines[0]
        self.assertEqual((start["action"], start["command"], start["dry_run"]),
                         ("start", "remove", True))
        [plan] = self.only(lines, "would_remove")
        self.assertEqual(plan["prefix"], A)
        self.assertEqual(plan["dataset_uuid"], UUID_A)
        self.assertEqual(plan["created"], CREATED)
        self.assertEqual(plan["depositors"], ["alice"])
        # one.csv x2, two.txt, gone.txt's version, the record = 5 versions
        # of 4 keys, plus gone.txt's delete marker.
        self.assertEqual((plan["objects"], plan["versions"], plan["delete_markers"]),
                         (4, 5, 1))
        self.assertEqual(plan["bytes"], 4 + 4 + 5 + 1 + len(body))
        self.assertEqual(plan["referenced_by"], 1)
        [rem] = self.only(lines, "remaining")
        self.assertEqual((rem["datasets"], rem["count"]), ([B], 1))
        self.assertEqual(self.only(lines, "finish")[-1]["nothing_done"], True)
        self.assertEqual(self.versions(A) + self.versions(B), before)


class TestRemove(RemoveBase):
    def test_every_version_goes_and_the_sibling_stays(self):
        _, body = self.place("promo", UUID_A)
        self.place("promo-b", UUID_B)
        b_before = self.versions(B)
        self.assertEqual(len(b_before), 6)
        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A,
                                "--reason", "test data", "--by", "tester"])
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.versions(A), [])
        self.assertEqual(self.versions(B), b_before)
        [done] = self.only(lines, "dataset_removed")
        self.assertEqual(done["prefix"], A)
        self.assertEqual(done["dataset_uuid"], UUID_A)
        self.assertEqual((done["created"], done["depositors"]), (CREATED, ["alice"]))
        self.assertEqual((done["objects"], done["versions"], done["delete_markers"]),
                         (4, 5, 1))
        self.assertEqual(done["bytes"], 14 + len(body))
        self.assertEqual((done["reason"], done["by"]), ("test data", "tester"))
        [rem] = self.only(lines, "remaining")
        self.assertEqual(rem["datasets"], [B])
        fin = self.only(lines, "finish")[-1]
        self.assertEqual((fin["removed"], fin["refused_or_failed"], fin["dry_run"]),
                         (1, 0, False))

    def test_the_record_is_deleted_last_in_its_own_call(self):
        self.place("promo", UUID_A)
        calls = self.record_calls()
        code, _ = self.cli(["remove", "--dataset", A, "--confirm", A])
        self.assertEqual(code, 0)
        rk = A + "/dataset.promo.json"
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(set(calls[-1]), {rk})
        for earlier in calls[:-1]:
            self.assertNotIn(rk, earlier)

    def test_a_failure_after_the_members_leaves_the_record_and_a_retry_finishes(self):
        self.place("promo", UUID_A)
        rk = A + "/dataset.promo.json"
        orig = self.s3.delete_objects

        def flaky(**kw):
            if any(o["Key"] == rk for o in kw["Delete"]["Objects"]):
                raise OSError("connection reset")
            return orig(**kw)
        with mock.patch.object(self.s3, "delete_objects", side_effect=flaky):
            code, lines = self.cli(["remove", "--dataset", A, "--confirm", A])
        self.assertEqual(code, 1)
        self.assertEqual(self.only(lines, "dataset_removed"), [])
        self.assertEqual(len(self.only(lines, "remove_failed")), 1)
        left = self.versions(A)
        self.assertEqual([k for k, _ in left], [rk])        # members gone, record kept
        # The retry recognises the prefix as a dataset and finishes the job.
        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A])
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.versions(A), [])
        [done] = self.only(lines, "dataset_removed")
        self.assertEqual((done["objects"], done["versions"]), (1, 1))

    def test_something_left_after_the_deletes_is_incomplete_not_silent(self):
        self.place("promo", UUID_A)
        orig = self.s3.delete_objects

        def swallow(**kw):
            # Pretend the store accepted the members but kept one.
            objs = [o for o in kw["Delete"]["Objects"] if not o["Key"].endswith("/two.txt")]
            return orig(**dict(kw, Delete={"Objects": objs, "Quiet": True}))
        with mock.patch.object(self.s3, "delete_objects", side_effect=swallow):
            code, lines = self.cli(["remove", "--dataset", A, "--confirm", A])
        self.assertEqual(code, 1)
        [inc] = self.only(lines, "remove_incomplete")
        self.assertEqual(inc["prefix"], A)
        self.assertEqual(inc["left"], [A + "/two.txt"])
        self.assertEqual(sorted(k for k, _ in self.versions(A)),
                         [A + "/dataset.promo.json", A + "/two.txt"])

    def test_a_refused_identifier_does_not_stop_the_others(self):
        self.place("promo", UUID_A)
        c = "rs2/csac/green/0_raw/loose"
        self.s3.put_object(Bucket="crsw", Key=c + "/one.csv", Body=b"x")
        code, lines = self.cli(["remove", "--dataset", c, "--dataset", A,
                                "--confirm", A, "--confirm", c])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.only(lines, "remove_refused")), 1)
        self.assertEqual(len(self.only(lines, "dataset_removed")), 1)
        self.assertEqual(self.versions(A), [])
        self.assertEqual(len(self.versions(c)), 1)
        fin = self.only(lines, "finish")[-1]
        self.assertEqual((fin["removed"], fin["refused_or_failed"]), (1, 1))


class TestHiddenRecord(RemoveBase):
    """A dataset deleted by hand on a versioned bucket (plain deletes, so
    a marker on every key) is hidden, not gone. Its record's newest
    version still says what the prefix is, so remove takes it; a prefix
    that never had a record is still refused."""

    def hide(self, prefix):
        for key in sorted({k for k, _ in self.versions(prefix)}):
            self.s3.delete_object(Bucket="crsw", Key=key)

    def test_a_record_under_a_delete_marker_still_identifies_the_dataset(self):
        _, body = self.place("promo", UUID_A)
        self.place("promo-b", UUID_B)
        self.hide(A)
        self.assertEqual(self.s3.list_objects_v2(Bucket="crsw", Prefix=A + "/")
                         .get("KeyCount", 0), 0)
        code, lines = self.cli(["remove", "--dataset", A])
        self.assertEqual(code, 0, lines)
        [plan] = self.only(lines, "would_remove")
        self.assertTrue(plan["record_hidden"])
        self.assertEqual((plan["dataset_uuid"], plan["created"], plan["depositors"]),
                         (UUID_A, CREATED, ["alice"]))
        # 5 versions of 4 keys as before, now a marker on each of the 4
        # keys plus gone.txt's original one.
        self.assertEqual((plan["objects"], plan["versions"], plan["delete_markers"]),
                         (4, 5, 5))
        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A, "--by", "tester"])
        self.assertEqual(code, 0, lines)
        [done] = self.only(lines, "dataset_removed")
        self.assertTrue(done["record_hidden"])
        self.assertEqual(done["dataset_uuid"], UUID_A)
        self.assertEqual(self.versions(A), [])
        self.assertEqual(len(self.versions(B)), 6)

    def test_a_prefix_that_never_had_a_record_is_still_refused(self):
        c = "rs2/csac/green/0_raw/loose"
        self.s3.put_object(Bucket="crsw", Key=c + "/one.csv", Body=b"x")
        self.s3.delete_object(Bucket="crsw", Key=c + "/one.csv")
        code, lines = self.cli(["remove", "--dataset", c, "--confirm", c])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertIn("no record in place", refused["problems"][0])
        self.assertEqual(len(self.versions(c)), 2)

    def test_a_visible_record_is_not_marked_hidden(self):
        self.place("promo", UUID_A)
        code, lines = self.cli(["remove", "--dataset", A])
        [plan] = self.only(lines, "would_remove")
        self.assertFalse(plan["record_hidden"])


class TestDerivedLayers(RemoveBase):
    ENV2 = dict(LLM_ENV, PROMOTER_INDEX_PREFIX="index")

    def test_index_embeddings_and_passages_follow(self):
        self.place("promo", UUID_A)
        self.place("promo-b", UUID_B)
        code, _ = self.cli(["index", "--embed"], env=self.ENV2)
        self.assertEqual(code, 0)
        self.assertEqual(set(self.table()), {A, B})
        for ident in (A, B):
            self.s3.put_object(Bucket="crsw", Key="index/passages/%s.parquet" % ident,
                               Body=b"not read")
        self.s3.put_object(Bucket="crsw", Key="audit/promoter/sentinel", Body=b"keep")
        self.s3.put_object(Bucket="crsw", Key="index/other.txt", Body=b"keep")

        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A], env=self.ENV2)
        self.assertEqual(code, 0, lines)
        actions = [l["action"] for l in lines]
        self.assertLess(actions.index("dataset_removed"), actions.index("index_written"))
        [ix] = self.only(lines, "index_written")
        self.assertEqual(ix["datasets"], 1)
        jsonl = self.s3.get_object(Bucket="crsw", Key="index/datasets.jsonl")["Body"].read()
        self.assertEqual([json.loads(l)["identifier"] for l in jsonl.splitlines()], [B])
        [emb] = self.only(lines, "embeddings_written")
        self.assertEqual(emb["dropped"], 1)
        self.assertEqual(set(self.table()), {B})
        [pr] = self.only(lines, "passages_removed")
        self.assertEqual((pr["prefix"], pr["key"], pr["reason"]),
                         (A, "index/passages/%s.parquet" % A, "no record in place"))
        self.assertEqual(self.keys_under("index/passages/"),
                         ["index/passages/%s.parquet" % B])
        self.assertEqual(self.keys_under("audit/"), ["audit/promoter/sentinel"])
        self.assertIn("index/other.txt", self.keys_under("index/"))

    def test_no_index_prefix_means_nothing_derived_is_touched(self):
        self.place("promo", UUID_A)
        self.s3.put_object(Bucket="crsw", Key="index/datasets.jsonl", Body=b"stale")
        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A],
                               env={"PROMOTER_INDEX_PREFIX": ""})
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.only(lines, "index_written"), [])
        self.assertEqual(self.s3.get_object(Bucket="crsw", Key="index/datasets.jsonl")
                         ["Body"].read(), b"stale")

    def table(self):
        body = self.s3.get_object(Bucket="crsw", Key="index/embeddings.parquet")["Body"].read()
        return {r["identifier"]: r for r in pq.read_table(io.BytesIO(body)).to_pylist()}


class TestAudit(RemoveBase):
    AUDIT = {"PROMOTER_AUDIT_PREFIX": "audit/promoter"}

    def test_dry_and_real_runs_are_on_the_trail_and_audit_finds_the_removal(self):
        self.place("promo", UUID_A)
        code, lines = self.cli(["remove", "--dataset", A], env=self.AUDIT)
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["action"], "audit_written")
        code, lines = self.cli(["remove", "--dataset", A, "--confirm", A, "--by", "tester"],
                               env=self.AUDIT)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1]["action"], "audit_written")
        self.assertEqual(len(self.keys_under("audit/")), 2)
        code, lines = self.cli(["audit", "--dataset", A, "--json"], env=self.AUDIT)
        self.assertEqual(code, 0)
        found = self.only(lines, "dataset_removed")
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["dataset_uuid"], found[0]["by"]), (UUID_A, "tester"))
        code, lines = self.cli(["audit", "--user", "tester", "--json"], env=self.AUDIT)
        self.assertIn("dataset_removed", [l["action"] for l in lines])


class TestStaging(RemoveBase):
    def test_an_open_deposit_is_removed_control_object_last(self):
        dep = self.stage(finalise=False)
        other = self.stage(finalise=False, dataset="promo-b")
        root = self.store.root("k1078591", dep.id)
        before = self.versions(root)
        keys_before = sorted({k for k, _ in before})
        self.assertEqual(len(keys_before), 3)      # one.csv, sub/r.md, _deposit.json
        other_before = self.versions(self.store.root("k1078591", other.id))

        code, lines = self.cli(["remove", "--staging", dep.id])
        self.assertEqual(code, 0, lines)
        [plan] = self.only(lines, "would_remove_staging")
        self.assertEqual((plan["deposit"], plan["user"], plan["objects"], plan["versions"]),
                         (dep.id, "k1078591", 3, len(before)))
        self.assertEqual(self.versions(root), before)

        calls = self.record_calls()
        code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", dep.id,
                                "--by", "tester"])
        self.assertEqual(code, 0, lines)
        [done] = self.only(lines, "staging_removed")
        self.assertEqual((done["deposit"], done["user"], done["versions"], done["by"]),
                         (dep.id, "k1078591", len(before), "tester"))
        self.assertEqual(self.versions(root), [])
        self.assertEqual(self.versions(self.store.root("k1078591", other.id)), other_before)
        self.assertEqual(set(calls[-1]), {self.store.control_key("k1078591", dep.id)})
        self.assertEqual(self.only(lines, "index_written"), [])

    def test_a_finished_deposit_that_passes_its_checks_is_refused(self):
        dep = self.stage()
        root = self.store.root("k1078591", dep.id)
        before = self.versions(root)
        code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", dep.id])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertEqual(refused["deposit"], dep.id)
        self.assertIn("passes the promoter's checks: promote it", refused["problems"][0])
        self.assertEqual(self.versions(root), before)

    def test_a_finished_deposit_the_promoter_refuses_can_be_removed(self):
        # r12: a stale copy of the same size is refused by the re-hash; without
        # this it would sit in staging, re-checked and refused on every run.
        dep = self.stage()
        root = self.store.root("k1078591", dep.id)
        member = self.store.staged_key("k1078591", dep.id, dep.prefix + "/one.csv")
        labels = self.s3.head_object(Bucket="crsw", Key=member)["Metadata"]
        self.s3.put_object(Bucket="crsw", Key=member, Body=b"x,y\n9,9\n",   # same size, other bytes
                           Metadata=labels)
        code, lines = self.cli(["remove", "--staging", dep.id])
        self.assertEqual(code, 0, lines)
        [plan] = self.only(lines, "would_remove_staging")
        self.assertIn("stored bytes hash to", plan["refused_because"][0])
        code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", dep.id, "--by", "tester"])
        self.assertEqual(code, 0, lines)
        [done] = self.only(lines, "staging_removed")
        self.assertIn("refused_because", done)
        self.assertEqual(self.versions(root), [])

    def test_a_finished_deposit_refused_for_the_promoters_configuration_is_kept(self):
        # The authorisation map, a size cap or a vocabulary fetched without the
        # record's provider would all refuse a deposit that promotes once the
        # VM is set right: removal is for the deposit's own content only.
        dep = self.stage()
        root = self.store.root("k1078591", dep.id)
        before = self.versions(root)
        for env in ({"PROMOTER_AUTHORISED": '{"nobody": ["rs2"]}'},
                    {"PROMOTER_MAX_OBJECT_BYTES": "3"}):
            code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", dep.id], env=env)
            self.assertEqual(code, 1, lines)
            [refused] = self.only(lines, "remove_refused")
            self.assertIn("not for its own bytes or record", refused["problems"][0])
            self.assertIn("Refused because:", refused["problems"][0])
            self.assertEqual(self.versions(root), before)
        # Both at once: the content problem does not outweigh the configuration one.
        member = self.store.staged_key("k1078591", dep.id, dep.prefix + "/one.csv")
        labels = self.s3.head_object(Bucket="crsw", Key=member)["Metadata"]
        self.s3.put_object(Bucket="crsw", Key=member, Body=b"x,y\n9,9\n", Metadata=labels)
        code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", dep.id],
                               env={"PROMOTER_AUTHORISED": '{"nobody": ["rs2"]}'})
        self.assertEqual(code, 1, lines)
        self.assertNotEqual(self.versions(root), [])

    def test_an_unknown_id_is_refused(self):
        code, lines = self.cli(["remove", "--staging", "nope", "--confirm", "nope"])
        self.assertEqual(code, 1)
        [refused] = self.only(lines, "remove_refused")
        self.assertIn("no such deposit in staging", refused["problems"][0])

    def test_confirm_applies_to_staging_too(self):
        dep = self.stage(finalise=False)
        root = self.store.root("k1078591", dep.id)
        before = self.versions(root)
        code, lines = self.cli(["remove", "--staging", dep.id, "--confirm", "other"])
        self.assertEqual(code, 2)
        self.assertEqual(self.versions(root), before)


if __name__ == "__main__":
    unittest.main()
