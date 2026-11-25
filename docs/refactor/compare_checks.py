"""Compare promoter.checks.check on this checkout with the same function
on another commit (default `main`), over many deliberately damaged
staged deposits.

Run from the repository root with the project's virtual environment
(it needs moto, boto3 and FastAPI, as the promoter tests do):

    .venv/Scripts/python docs/refactor/compare_checks.py [BASE]

Each case stages a real deposit through the web service against a
mocked store, damages it in one or more ways, then gives the same
deposit to both versions of check(). The whole Report (problems,
warnings and every other field) must be identical. check() changes
nothing in the store, so both versions see the same state. Exit code 1
if any case differs.

The other commit's checks.py is loaded beside this checkout's promoter
package, so it uses this checkout's crsw_deposit and promoter.config;
only checks.py itself is the old one.
"""
import copy
import dataclasses
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE_REF = sys.argv[1] if len(sys.argv) > 1 else "main"

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

from crsw_deposit import authority, record, vocab  # noqa: E402
from crsw_web.app import create_app  # noqa: E402
from crsw_web.config import Settings  # noqa: E402
from crsw_web.deposits import DepositStore  # noqa: E402
from promoter import checks as new_checks  # noqa: E402
from promoter.config import PromoterConfig  # noqa: E402

old_src = subprocess.run(["git", "show", BASE_REF + ":promoter/checks.py"],
                         cwd=str(ROOT), capture_output=True, check=True).stdout
old_path = Path(tempfile.mkdtemp()) / "checks_base.py"
old_path.write_bytes(old_src)
spec = importlib.util.spec_from_file_location("promoter.checks_base", str(old_path))
old_checks = importlib.util.module_from_spec(spec)
sys.modules["promoter.checks_base"] = old_checks
spec.loader.exec_module(old_checks)

VOCAB = json.loads((ROOT / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
SETTINGS = dict(s3_endpoint="https://rgw.example", s3_access_key="t",
                s3_secret_key="t", s3_bucket="crsw", staging_prefix="staging/_test")
CFG = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
           s3_bucket="crsw", staging_prefix="staging/_test", log_path="")
FORM = {"strand": "rs2", "sensitivity": "green", "state": "0_raw",
        "project": "csac", "domain": "quant", "version": "1-0",
        "coverage_start": "2020", "coverage_end": "2021",
        "subject": ["forced-labour"], "abstract": "words " * 60}
USER = "k1234567"
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 57
NOW = "2026-11-24T10:00:00Z"


def evolved(kind):
    """The bundled vocabulary after a merge, a split or a retirement."""
    change = {"date": "2026-10-01", "by": "k1"}
    if kind == "merge":
        return authority.apply_change(VOCAB, dict(
            change, kind="merge", **{"from": ["debt-bondage"]}, to=["forced-labour"]))
    if kind == "retire":
        return authority.apply_change(VOCAB, dict(
            change, kind="retire", **{"from": ["debt-bondage"]}))
    d = VOCAB
    for slug in ("survey-household", "survey-online"):
        d = authority.apply_change(d, dict(change, kind="add", to=[slug],
                                           facet="methods", label=slug))
    return authority.apply_change(d, dict(
        change, kind="split", **{"from": ["survey"]},
        to=["survey-household", "survey-online"]))


class Case:
    """One staged deposit and the arguments check() will get."""

    def __init__(self, s3, web, store, n, files=None, unchecked=False, **form):
        self.s3, self.store = s3, store
        self.cfg = PromoterConfig(**CFG)
        self.kw = {}
        self.vocab_doc = None
        files = files or (("one.csv", b"a,b\n1,2\n"), ("sub/r.md", b"hi\n"))
        body = dict(FORM, dataset="case-%d" % n, **form)
        patches = []
        if unchecked:
            patches = [mock.patch("crsw_deposit.filetypes.refused_name", return_value=None),
                       mock.patch("crsw_deposit.filetypes.refused_content",
                                  return_value=None)]
        for p in patches:
            p.start()
        try:
            d = web.post("/deposits", json=body).json()
            for member, data in files:
                r = web.put("/deposits/%s/files/%s" % (d["id"], member), content=data)
                assert r.status_code == 200, r.text
            r = web.post("/deposits/%s/finalise" % d["id"])
            assert r.status_code == 200, r.text
        finally:
            for p in patches:
                p.stop()
        self.dep = store.load(USER, d["id"])
        self.staged = store.staged_key(USER, self.dep.id, self.dep.prefix)

    # -- helpers the damages use
    def staged_record(self):
        body = self.s3.get_object(Bucket="crsw", Key=self.dep.record_key)["Body"].read()
        return json.loads(body.decode("utf-8"))

    def put_staged_record(self, rec):
        self.s3.put_object(Bucket="crsw", Key=self.dep.record_key,
                           Body=record.record_json(rec).encode("utf-8"))

    def edit_staged_record(self, **fields):
        rec = self.staged_record()
        rec.update(fields)
        self.put_staged_record(rec)

    def member_key(self, i=0):
        return self.staged + "/" + self.dep.entries[i]["path"]

    def rewrite_member(self, data, i=0, metadata=None):
        key = self.member_key(i)
        meta = self.s3.head_object(Bucket="crsw", Key=key).get("Metadata", {})
        self.s3.put_object(Bucket="crsw", Key=key, Body=data,
                           Metadata=meta if metadata is None else metadata)

    def put_destination(self, mutate=None, raw=None):
        key = self.dep.prefix + "/" + "dataset.%s.json" % self.dep.meta["dataset"]
        if raw is None:
            rec = self.staged_record()
            rec["dataset_uuid"] = "11111111-1111-4111-8111-111111111111"
            rec["files"] = [record.manifest_entry("old/a.csv", "b" * 64, 3)]
            if mutate:
                mutate(rec)
            raw = record.record_json(rec).encode("utf-8")
        self.s3.put_object(Bucket="crsw", Key=key, Body=raw)


# ---- damages: each takes a Case and changes the deposit, the store or
# ---- the arguments. Any combination must give the same answer twice.

def d_status(c): c.dep.status = "open"
def d_no_record_key(c): c.dep.record_key = None
def d_no_entries(c): c.dep.entries = []
def d_record_key_elsewhere(c):
    body = c.s3.get_object(Bucket="crsw", Key=c.dep.record_key)["Body"].read()
    c.dep.record_key = c.staged + "/elsewhere.json"
    c.s3.put_object(Bucket="crsw", Key=c.dep.record_key, Body=body)
def d_record_missing(c): c.s3.delete_object(Bucket="crsw", Key=c.dep.record_key)
def d_record_not_json(c):
    c.s3.put_object(Bucket="crsw", Key=c.dep.record_key, Body=b"{not json")
def d_record_future_schema(c): c.edit_staged_record(schema_version="9.9")
def d_record_old_schema(c):
    rec = c.staged_record()
    rec["schema_version"] = "0.6"
    rec["domain"] = rec["domain"][0]
    c.put_staged_record(rec)
def d_record_unknown_subject(c): c.edit_staged_record(subject=["no-such-term"])
def d_record_identifier(c): c.edit_staged_record(identifier="rs2/csac/green/0_raw/other")
def d_record_uuid(c):
    c.edit_staged_record(dataset_uuid="22222222-2222-4222-8222-222222222222")
def d_record_manifest(c):
    rec = c.staged_record()
    rec["files"] = rec["files"][:1]
    c.put_staged_record(rec)
def d_record_no_files(c): c.edit_staged_record(files=[])
def d_stale_merge(c):
    c.edit_staged_record(subject=["debt-bondage"])
    c.vocab_doc = evolved("merge")
def d_stale_retire(c):
    c.edit_staged_record(subject=["debt-bondage", "forced-labour"])
    c.vocab_doc = evolved("retire")
def d_stale_split(c):
    c.edit_staged_record(subject=["survey"])
    c.vocab_doc = evolved("split")
def d_vocab_doc_plain(c): c.vocab_doc = VOCAB
def d_derived_absent(c):
    c.edit_staged_record(derived_from=[
        {"kind": "dataset", "identifier": "rs1/none/green/2_final/gone"},
        {"kind": "external", "url": "https://example.org/x"}])
def d_derived_present(c):
    parent = c.staged_record()
    parent.update(identifier="rs1/p/green/2_final/parent", strand="rs1", project="p",
                  state="2_final", dataset="parent",
                  dataset_uuid="33333333-3333-4333-8333-333333333333")
    c.s3.put_object(Bucket="crsw", Key="rs1/p/green/2_final/parent/dataset.parent.json",
                    Body=record.record_json(parent).encode("utf-8"))
    c.edit_staged_record(derived_from=[
        {"kind": "dataset", "identifier": "rs1/p/green/2_final/parent"}])
def d_derived_wrong_shape(c): c.edit_staged_record(derived_from="a string")
def d_member_missing(c): c.s3.delete_object(Bucket="crsw", Key=c.member_key(0))
def d_member_size(c): c.rewrite_member(b"different length entirely\n")
def d_member_content(c):
    size = c.dep.entries[0]["bytes"]
    c.rewrite_member(b"z" * size)
def d_member_program_bytes(c):
    size = c.dep.entries[0]["bytes"]
    c.rewrite_member((ELF + b"x" * size)[:size] if size >= 4 else b"z" * size)
def d_label_wrong(c):
    key = c.member_key(-1)
    head = c.s3.head_object(Bucket="crsw", Key=key)
    meta = dict(head.get("Metadata", {}), depositor="somebody-else")
    body = c.s3.get_object(Bucket="crsw", Key=key)["Body"].read()
    c.s3.put_object(Bucket="crsw", Key=key, Body=body, Metadata=meta)
def d_labels_gone(c):
    key = c.member_key(0)
    body = c.s3.get_object(Bucket="crsw", Key=key)["Body"].read()
    c.s3.put_object(Bucket="crsw", Key=key, Body=body)
def d_max_object(c): c.cfg = dataclasses.replace(c.cfg, max_object_bytes=3)
def d_max_deposit(c): c.cfg = dataclasses.replace(c.cfg, max_deposit_bytes=5)
def d_not_authorised(c): c.cfg = dataclasses.replace(c.cfg, authorised={"other": ["rs1"]})
def d_authorised_other_strand(c):
    c.cfg = dataclasses.replace(c.cfg, authorised={USER: ["rs1"]})
def d_no_checksums(c): c.kw["verify_checksums"] = False
def d_providers(c):
    c.kw["provider_slugs"] = ["acled"]
    c.edit_staged_record(provider=["nobody"])
def d_places(c):
    c.kw["place_codes"] = {"iso:GB"}
    c.edit_staged_record(spatial=["iso:GB", "m49:999"])
def d_entry_path_backslash(c): c.dep.entries[0]["path"] = "a\\b.csv"
def d_entry_path_reserved(c): c.dep.entries[-1]["path"] = "sub/dataset.x.json"
def d_entry_path_program(c): c.dep.entries[0]["path"] = "setup.exe"
def d_entry_checksum(c): c.dep.entries[0]["checksum_sha256"] = "c" * 64
def d_entry_bytes(c): c.dep.entries[0]["bytes"] += 1
def d_dest_merge(c): c.put_destination()
def d_dest_not_json(c): c.put_destination(raw=b"<html>")
def d_dest_future(c): c.put_destination(lambda r: r.update(schema_version="9.9"))
def d_dest_old_schema(c):
    def older(r):
        r["schema_version"] = "0.6"
        r["domain"] = r["domain"][0]
    c.put_destination(older)
def d_dest_moved(c): c.put_destination(lambda r: r.update(identifier="rs2/csac/green/0_raw/was"))
def d_dest_other_dataset(c): c.put_destination(lambda r: r.update(dataset="was"))
def d_dest_overlapping(c):
    def overlap(r):
        r["files"] = [record.manifest_entry("one.csv", "d" * 64, 9),
                      record.manifest_entry("old/a.csv", "b" * 64, 3)]
    c.put_destination(overlap)


DAMAGES = [v for k, v in sorted(globals().items()) if k.startswith("d_")]
FILE_SETS = [
    dict(),
    dict(files=(("one.csv", b"a,b\n"), ("tools/setup.exe", b"anything")), unchecked=True),
    dict(files=(("one.csv", b"a,b\n"), ("notes.txt", ELF + b"x" * 900)), unchecked=True),
    dict(files=(("setup.exe", b"MZ\x90\x00" + b"\x00" * 60),), unchecked=True),
    dict(files=(("empty.txt", b""), ("notes.txt", ELF)), unchecked=True),
    dict(files=(("clean.py", b"#!/usr/bin/env python\nprint(1)\n"),
                ("data.csv", b"MZ,2019,31.2\n"), ("all.zip", b"PK\x03\x04rest"),
                ("run.txt", b"#!/bin/sh\necho hi\n")), unchecked=True),
    dict(files=tuple(("z%d.zip" % i, b"PK") for i in range(7))),
    dict(sensitivity="amber"),
]


def run(fn, case):
    terms = vocab.all_terms(case.vocab_doc or VOCAB)
    codes = vocab.domain_codes(case.vocab_doc or VOCAB)
    kw = dict(case.kw)
    if case.vocab_doc is not None:
        kw["vocab_doc"] = case.vocab_doc
    try:
        rep = fn(copy.deepcopy(case.dep), case.store, case.cfg, terms, codes, **kw)
        return ("ok", dataclasses.asdict(rep))
    except Exception as e:   # the same exception must come from both
        return ("raised", type(e).__name__, str(e))


def main():
    env = {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
           "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""}
    rng = random.Random(20261124)
    plans = [(fs, [d]) for fs in (0,) for d in DAMAGES]
    plans += [(i, []) for i in range(len(FILE_SETS))]
    plans += [(i, [d_no_checksums]) for i in range(len(FILE_SETS))]
    for _ in range(400):
        plans.append((rng.randrange(len(FILE_SETS)),
                      rng.sample(DAMAGES, rng.randint(2, 4))))
    cases = differ = clean = raised = 0
    seen_problems = set()
    with mock.patch.dict(os.environ, env), mock_aws(), \
            mock.patch("crsw_deposit.record.utc_now_iso", return_value=NOW):
        s3 = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="t", aws_secret_access_key="t")
        s3.create_bucket(Bucket="crsw")
        web = TestClient(create_app(Settings(**SETTINGS), s3_client=s3, vocab_dict=VOCAB))
        store = DepositStore(s3, "crsw", "staging/_test")
        for n, (file_set, damages) in enumerate(plans):
            case = Case(s3, web, store, n, **FILE_SETS[file_set])
            names = []
            for damage in damages:
                try:
                    damage(case)
                    names.append(damage.__name__)
                except Exception as e:    # a damage that no longer applies
                    names.append("%s (skipped: %s)" % (damage.__name__, type(e).__name__))
            a, b = run(old_checks.check, case), run(new_checks.check, case)
            cases += 1
            if a[0] == "raised":
                raised += 1
            elif not a[1]["problems"] and not a[1]["warnings"]:
                clean += 1
            else:
                for line in a[1]["problems"]:
                    seen_problems.add(line.split(":")[0][:28])
            if a != b:
                differ += 1
                if differ <= 3:
                    print("DIFFERENT: file set %d, %s\n  old: %s\n  new: %s"
                          % (file_set, names, a, b))
    print("checks.check: %d cases (%d clean, %d raised an exception), "
          "%d kinds of problem seen, %d differ"
          % (cases, clean, raised, len(seen_problems), differ))
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
