"""Send the same requests to the web app from another commit (default
`main`) and to the app on this checkout, and compare every response.

Run from the repository root with the project's virtual environment:

    .venv/Scripts/python docs/refactor/compare_web.py [BASE] [--show N]

How it works:
- The other commit's whole `crsw_web` package is unpacked into a
  temporary folder under the name `crsw_web_base`, templates and static
  files included. (Its `crsw_deposit` is this checkout's: one process
  cannot hold two. `crsw_web_base/config.py` also reads one string
  constant from this checkout's `crsw_web.llm`.)
- Each app gets its own bucket in one mocked store, seeded identically:
  four dataset records in place, the promoter's index, embeddings and
  passages. Deposits therefore never see the other app's staging.
- The apps are built under several configurations (read role off and
  on, search on, platform failing, proxy sign-in, small limits), and a
  scripted list of requests is sent to both, error cases included.
- Status, the headers that matter and the body must be identical, after
  replacing the things that differ by design: the bucket name, deposit
  ids, new UUIDs and timestamps.

Exit code 1 if any response differs.
"""
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
argv = sys.argv[1:]
SHOW = 5
if "--show" in argv:
    at = argv.index("--show")
    SHOW = int(argv[at + 1])
    del argv[at:at + 2]
BASE_REF = argv[0] if argv else "main"

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

from crsw_deposit import deposit_logic, record  # noqa: E402
from promoter import embed as embed_mod  # noqa: E402
from promoter import index as index_mod  # noqa: E402
from promoter import passages as pmod  # noqa: E402

TMP = Path(tempfile.mkdtemp())
archive = subprocess.run(["git", "archive", BASE_REF, "crsw_web"], cwd=str(ROOT),
                         stdout=subprocess.PIPE, check=True).stdout
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(str(TMP))
(TMP / "crsw_web").rename(TMP / "crsw_web_base")
sys.path.insert(0, str(TMP))

PKG = {"old": "crsw_web_base", "new": "crsw_web"}
BUCKET = {"old": "crsw-old", "new": "crsw-new"}
VOCAB = json.loads((ROOT / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
DIM = 8
USER = "k1234567"
GREEN = "rs2/csac/green/0_raw/events"
AMBER = "rs1/oral/amber/1_interim/interviews"
CLEAN = "rs2/csac/green/2_final/csac-clean"
CENTRE = "centre/cdisaw/green/2_final/acled"
SCANS = "rs2/csac/green/1_interim/events"
META = {"strand": "rs2", "sensitivity": "green", "state": "0_raw", "project": "csac",
        "dataset": "events", "domain": "quant", "version": "1-0",
        "coverage_start": "2020", "coverage_end": "2021", "subject": ["forced-labour"],
        "abstract": "Events from the CSAC extract, cleaned. " * 6, "creator": "Ada"}
FORM = {"strand": "rs2", "sensitivity": "green", "state": "0_raw", "project": "csac",
        "dataset": "compare", "domain": "quant", "version": "1-0",
        "coverage_start": "2020", "coverage_end": "2021",
        "subject": ["forced-labour"], "abstract": "words " * 60}
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 57


def bag(text):
    v = [0.0] * DIM
    for word in text.lower().split():
        v[sum(ord(c) for c in word) % DIM] += 1.0
    return v


def fake_platform(error_class, fail=(), off=()):
    """A platform stand-in whose failures are the given package's own
    PlatformError, so each app catches what it expects to catch."""
    class FakePlatform:
        chat_model, embed_model, rerank_model = "arc:lite", "arc:embedvl", "arc:rerankvl"
        ocr_model = ""

        def embed_fingerprint(self):
            return "toy"

        def embed(self, texts, batch=32, instruction=None):
            if "embed" in fail:
                raise error_class("platform unreachable: ConnectError")
            return [bag(t) for t in texts]

        def explain(self, question, passage):
            if "explain" in fail:
                raise error_class("platform returned HTTP 503 for /chat/completions")
            return "It mentions %s." % passage.split()[0]

        def rerank(self, question, documents):
            if "rerank" in fail:
                raise error_class("platform returned HTTP 404 for /rerank")
            return list(range(len(documents)))[::-1]

        def close(self):
            pass

    p = FakePlatform()
    for name in off:
        setattr(p, name + "_model", "")
    return p


# ------------------------------------------------------------- seeding

def put_record(s3, bucket, identifier, meta, entries, depositor, uuid, **extra):
    rec, _, _, _ = deposit_logic.assemble_record(
        meta, None, entries, depositor, "2026-09-01T00:00:00Z", uuid)
    rec.update(extra)
    key = identifier + "/dataset.%s.json" % meta["dataset"]
    s3.put_object(Bucket=bucket, Key=key, Body=deposit_logic.record_bytes(rec),
                  Metadata={"depositor": depositor})
    for e in entries:
        s3.put_object(Bucket=bucket, Key=identifier + "/" + e["path"], Body=b"x" * e["bytes"])


def put_member(s3, bucket, identifier, member, data):
    key = identifier + "/dataset.%s.json" % identifier.rsplit("/", 1)[-1]
    rec = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    files = [f for f in rec["files"] if f["path"] != member]
    files.append({"path": member, "checksum_sha256": hashlib.sha256(data).hexdigest(),
                  "bytes": len(data), "format": member.rsplit(".", 1)[-1]})
    rec["files"] = sorted(files, key=lambda f: f["path"])
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(rec).encode(),
                  Metadata={"depositor": rec["depositors"][0]})
    s3.put_object(Bucket=bucket, Key=identifier + "/" + member, Body=data)


def seed(s3, bucket):
    s3.create_bucket(Bucket=bucket)
    put_record(s3, bucket, GREEN, META,
               [record.manifest_entry("events.csv", "a" * 64, 1200),
                record.manifest_entry("notes/readme.md", "b" * 64, 40)],
               USER, "11111111-1111-4111-8111-111111111111",
               provenance=[{"activity": "clean", "tool": {
                   "name": "clean.py", "repo": "https://x.org/r", "commit": "abc1234"}}])
    put_record(s3, bucket, AMBER,
               dict(META, strand="rs1", sensitivity="amber", state="1_interim",
                    project="oral", dataset="interviews", subject=["forced-marriage"],
                    abstract="Interview transcripts, anonymised. " * 6, creator="Bob"),
               [record.manifest_entry("t1.txt", "c" * 64, 9)],
               "k2", "22222222-2222-4222-8222-222222222222",
               derived_from=[{"kind": "dataset", "identifier": GREEN,
                              "dataset_uuid": "11111111-1111-4111-8111-111111111111",
                              "version": "1-0"},
                             {"kind": "external", "url": "https://example.org/source"}])
    put_record(s3, bucket, CLEAN,
               dict(META, state="2_final", dataset="csac-clean", subject=["armed-conflict"],
                    coverage_start="1989", coverage_end="2025", creator="Cy",
                    abstract="Conflict deaths per year, cleaned from the CSAC database. " * 4),
               [record.manifest_entry("clean.csv", "d" * 64, 300)],
               "k3", "33333333-3333-4333-8333-333333333333",
               spatial=["iso:GB", "m49:002"], spatial_note="Mostly the north.",
               license="CC-BY-4.0", notes="A note.")
    put_record(s3, bucket, CENTRE,
               dict(META, strand="centre", project="cdisaw", state="2_final",
                    dataset="acled", subject=["armed-conflict"], creator="Centre",
                    abstract="Armed conflict events harmonised for the corpus. " * 5),
               [record.manifest_entry("events.parquet", "e" * 64, 64)],
               USER, "44444444-4444-4444-8444-444444444444")
    # The same dataset name as GREEN at another state, holding images:
    # for the one-name accordion, the file type filter and the viewer.
    put_record(s3, bucket, SCANS, dict(META, state="1_interim"),
               [record.manifest_entry("pages/p 1.PNG", "f" * 64, 12),
                record.manifest_entry("p2.jpg", "1" * 64, 8),
                record.manifest_entry("drawing.svg", "2" * 64, 5)],
               USER, "55555555-5555-4555-8555-555555555555")
    put_member(s3, bucket, CLEAN, "method.txt",
               b"Conflict deaths are counted per year from the CSAC database. "
               b"Each event is coded by location and actor.\n")
    put_member(s3, bucket, GREEN, "notes/readme.md",
               b"# Events\nThe events file lists battles and massacres with dates.\n")
    put_member(s3, bucket, AMBER, "t1.txt", b"Interview with a survivor of forced marriage.\n")
    put_member(s3, bucket, GREEN, "notes/method.md",
               b"# Method\nFields populate automatically where possible; the rest are "
               b"typed. Dates were checked against the archive.\n")
    rows = index_mod.rows(s3, bucket)
    index_mod.write(s3, bucket, "index", rows)
    platform = fake_platform(RuntimeError)
    embed_mod.refresh(s3, bucket, "index", rows, platform, "toy", {"green"})
    for ident in (CLEAN, GREEN):
        key = ident + "/dataset.%s.json" % ident.rsplit("/", 1)[-1]
        rec = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        pmod.refresh(s3, bucket, "index", ident, rec, platform, "toy", None,
                     50 * 1024 * 1024, 1000)


# ------------------------------------------------------- configurations

BASE = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
            staging_prefix="staging/_test", user_max_open_deposits=50)
READ = dict(read_s3_access_key="r", read_s3_secret_key="r")
LLM = dict(READ, llm_base_url="https://ai.example/api/v1", llm_api_key="sk-test",
           llm_embed_dims=DIM)
CONFIGS = [
    ("read role off", dict(), {}),
    ("read role on", dict(READ), {}),
    ("read on, amber for all, explorer link",
     dict(READ, amber_access="all", cdisaw_url="https://explorer.example"), {}),
    ("search on", dict(LLM, passages=True), dict(platform=())),
    ("search on, no passages", dict(LLM), dict(platform=())),
    ("search on, embed fails", dict(LLM, passages=True), dict(platform=("embed",))),
    ("search on, rerank and explain fail", dict(LLM, passages=True),
     dict(platform=("rerank", "explain"))),
    ("search on, reranker and chat off", dict(LLM, passages=True),
     dict(platform=(), off=("rerank", "chat"))),
    ("proxy sign-in", dict(READ, auth_mode="proxy", proxy_user_header="x-webauth-user",
                           proxy_name_header="x-webauth-name",
                           trusted_proxy_cidrs=("0.0.0.0/0",)), {}),
    ("proxy sign-in, peer not trusted",
     dict(auth_mode="proxy", proxy_user_header="x-webauth-user",
          trusted_proxy_cidrs=("10.9.9.0/24",)), {}),
    ("header diagnostic on", dict(debug_headers=True), {}),
    ("small limits", dict(READ, max_body_bytes=40, user_quota_bytes=60,
                          user_max_open_deposits=2, max_members_per_deposit=2), {}),
    ("another user", dict(READ, dev_user="k7654321"), {}),
]


def build(which, s3, extra, opts):
    pkg = PKG[which]
    app_mod = importlib.import_module(pkg + ".app")
    config = importlib.import_module(pkg + ".config")
    s3mod = importlib.import_module(pkg + ".s3")
    llm = importlib.import_module(pkg + ".llm")
    passages = importlib.import_module(pkg + ".passages")
    values = dict(BASE, s3_bucket=BUCKET[which], **extra)
    kw = dict(s3_client=s3, vocab_dict=VOCAB)
    if values.get("read_s3_access_key"):
        kw["read_client"] = s3mod.ReadOnly(s3)
    if "platform" in opts:
        kw["platform"] = fake_platform(llm.PlatformError, fail=opts["platform"],
                                       off=opts.get("off", ()))
        if values.get("passages"):
            path = str(TMP / ("passages-%s-%d.duckdb" % (which, build.count)))
            values["passages_path"] = path
            kw["passage_index"] = passages.PassageIndex(
                s3mod.ReadOnly(s3), BUCKET[which], "index", path)
    build.count += 1
    return TestClient(app_mod.create_app(config.Settings(**values), **kw),
                      raise_server_exceptions=False)


build.count = 0


# ------------------------------------------------------------- requests

def G(path, **kw): return ("GET", path, kw)
def P(path, **kw): return ("POST", path, kw)
def U(path, data, **kw): return ("PUT", path, dict(kw, content=data))
def X(path, **kw): return ("DELETE", path, kw)


SERVICE = [
    G("/"), G("/health"), G("/whoami"), G("/privacy"), G("/auth/headers"),
    G("/connectivity"), G("/vocabulary"), G("/openapi.json"), G("/docs"),
    G("/static/deposit.css"), G("/static/nope.css"), G("/nope"), P("/health"),
    ("HEAD", "/health", {}), G("/", headers={"x-webauth-user": "k1111111"}),
    G("/whoami", headers={"x-webauth-user": "K2222222@kcl.ac.uk",
                          "x-webauth-name": "Ada Lovelace"}),
    G("/whoami", headers={"x-webauth-user": "  "}),
    G("/auth/headers", headers={"x-webauth-user": "k1111111", "cookie": "a=b",
                                "authorization": "Bearer secret"}),
]

FIND = [
    G("/datasets"), G("/datasets?q=conflict"), G("/datasets?q=CSAC+events"),
    G("/datasets?strand=rs2"), G("/datasets?strand=centre"), G("/datasets?strand=rs9"),
    G("/datasets?state=2_final"), G("/datasets?sensitivity=amber"),
    G("/datasets?subject=armed-conflict"), G("/datasets?subject=no-such-term"),
    G("/datasets?subject=forced-labour&project=csac"), G("/datasets?project=oral"),
    G("/datasets?place=iso:GB"), G("/datasets?place=m49:002"), G("/datasets?place=nowhere"),
    G("/datasets?page=2&per=10"), G("/datasets?per=25"), G("/datasets?per=7"),
    G("/datasets?page=0"), G("/datasets?page=abc"), G("/datasets?per=abc"),
    G("/datasets", cookies={"crsw_per": "50"}), G("/datasets", cookies={"crsw_per": "x"}),
    G("/datasets?q=" + "x" * 600), G("/datasets?q=%3Cscript%3E"),
    G("/datasets.json"), G("/datasets.json?q=conflict"), G("/datasets.json?strand=rs1"),
    G("/datasets.json?subject=armed-conflict&place=iso:GB"),
    G("/datasets.json?sensitivity=green&state=0_raw&project=csac"),
    G("/datasets/" + GREEN), G("/datasets/" + AMBER), G("/datasets/" + CLEAN),
    G("/datasets/" + CENTRE), G("/datasets/rs2/csac/green/0_raw/missing"),
    G("/datasets/not/an/identifier"), G("/datasets/rs2"), G("/datasets/"),
    G("/datasets/" + GREEN + "/record"), G("/datasets/" + AMBER + "/record"),
    G("/datasets/rs2/csac/green/0_raw/missing/record"), G("/datasets/x/record"),
    G("/datasets/" + GREEN + "/files/events.csv"),
    G("/datasets/" + GREEN + "/files/notes/readme.md"),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "bytes=0-9"}),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "bytes=100-"}),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "bytes=-5"}),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "bytes=5000-6000"}),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "lines=1-2"}),
    G("/datasets/" + GREEN + "/files/events.csv", headers={"range": "bytes=9-1"}),
    ("HEAD", "/datasets/" + GREEN + "/files/events.csv", {}),
    G("/datasets/" + GREEN + "/files/nope.csv"),
    G("/datasets/" + GREEN + "/files/dataset.events.json"),
    G("/datasets/" + GREEN + "/files/../../../etc/passwd"),
    G("/datasets/" + AMBER + "/files/t1.txt"),
    G("/datasets/" + CENTRE + "/files/events.parquet"),
    G("/datasets/rs2/csac/green/0_raw/missing/files/a.csv"),
    G("/datasets?filetype=csv"), G("/datasets?filetype=.PNG"), G("/datasets?filetype=exe"),
    G("/datasets?filetype=jpg&strand=rs2&page=2"), G("/datasets?filetype=%3Cb%3E"),
    G("/datasets?project=csac"), G("/datasets.json?filetype=svg"),
    G("/datasets/" + SCANS),
    G("/datasets/" + SCANS + "/files/p2.jpg?inline=1"),
    G("/datasets/" + SCANS + "/files/pages/p%201.PNG?inline=1"),
    G("/datasets/" + SCANS + "/files/p2.jpg?inline=1", headers={"range": "bytes=0-3"}),
    G("/datasets/" + SCANS + "/files/p2.jpg?inline=1", headers={"range": "bytes=900-"}),
    G("/datasets/" + SCANS + "/files/p2.jpg"), G("/datasets/" + SCANS + "/files/p2.jpg?inline=0"),
    G("/datasets/" + SCANS + "/files/p2.jpg?inline=yes"),
    G("/datasets/" + SCANS + "/files/drawing.svg?inline=1"),
    G("/datasets/" + SCANS + "/files/nope.png?inline=1"),
    G("/datasets/" + GREEN + "/files/events.csv?inline=1"),
    G("/datasets/" + AMBER + "/files/t1.txt?inline=1"),
    G("/deposits/lookup?strand=rs2&project=csac&sensitivity=green&state=0_raw&dataset=events"),
    G("/deposits/lookup?strand=rs2&project=csac&sensitivity=green&state=0_raw&dataset=new-one"),
    G("/deposits/lookup?strand=rs2&project=csac"), G("/deposits/lookup"),
    G("/deposits/lookup?strand=rs9&project=x&sensitivity=red&state=y&dataset=Z"),
]

SEARCH = [
    G("/search"), G("/search?q=conflict+deaths"), G("/search?q=conflict+deaths&sort=meaning"),
    G("/search?q=conflict+deaths&sort=sideways"), G("/search?q=forced+marriage+survivor"),
    G("/search?q=%22checked+against+the+archive%22"), G("/search?q=zzzz+qqqq"),
    G("/search?q=" + "word+" * 200), G("/search?q=%3Cb%3Ebold%3C/b%3E"),
    G("/search.json"), G("/search.json?q=conflict+deaths"),
    G("/search.json?q=conflict+deaths&limit=1"), G("/search.json?q=conflict&limit=0"),
    G("/search.json?q=conflict&limit=abc"), G("/search.json?q=conflict&limit=5000"),
    G("/search.json?q=battles+and+massacres&sort=meaning"),
    G("/search.json?q=battles&sort=words"), G("/search.json?q=battles&sort=nope"),
    G("/search/why"), G("/search/why?q=conflict"),
    G("/search/why?q=conflict+deaths&identifier=" + CLEAN + "&member=method.txt&position=0"),
    G("/search/why?q=dates&identifier=" + GREEN + "&member=notes/method.md&position=0"),
    G("/search/why?q=dates&identifier=" + GREEN + "&member=notes/method.md&position=99"),
    G("/search/why?q=dates&identifier=" + GREEN + "&member=nope.md&position=0"),
    G("/search/why?q=x&identifier=" + AMBER + "&member=t1.txt&position=0"),
    G("/search/why?q=x&identifier=rs2/none/green/0_raw/x&member=a.txt&position=0"),
    G("/search/why?q=x&identifier=" + CLEAN + "&member=method.txt&position=abc"),
]

DEPOSIT = [
    P("/deposits", json=dict(FORM)),                                   # -> {dep1}
    G("/deposits/{dep1}"), G("/deposits/{dep1}/summary"), G("/deposits/nope"),
    G("/deposits/nope/summary"),
    U("/deposits/{dep1}/files/one.csv", b"a,b\n1,2\n"),
    U("/deposits/{dep1}/files/one.csv", b"a,b\n1,2\n"),
    U("/deposits/{dep1}/files/one.csv", b"a,b\n3,4\n5,6\n"),
    U("/deposits/{dep1}/files/sub/dir/r.md", b"# hi\n"),
    U("/deposits/{dep1}/files/empty.txt", b""),
    U("/deposits/{dep1}/files/dataset.x.json", b"{}"),
    U("/deposits/{dep1}/files/sub/dataset.compare.json", b"{}"),
    U("/deposits/{dep1}/files/.DS_Store", b"x"),
    U("/deposits/{dep1}/files/__MACOSX/a.txt", b"x"),
    U("/deposits/{dep1}/files/tools/setup.exe", b"anything"),
    U("/deposits/{dep1}/files/notes.txt", ELF + b"x" * 900),
    U("/deposits/{dep1}/files/run.sh", b"#!/bin/sh\necho hi\n"),
    U("/deposits/{dep1}/files/clean.py", b"#!/usr/bin/env python\nprint(1)\n"),
    U("/deposits/{dep1}/files/all.zip", b"PK\x03\x04rest"),
    U("/deposits/{dep1}/files/a%5Cb.csv", b"x"),
    U("/deposits/{dep1}/files/a%20b%3F.csv", b"x"),
    U("/deposits/{dep1}/files/caf%C3%A9.txt", b"x"),
    U("/deposits/{dep1}/files/cafe%CC%81.txt", b"y"),
    U("/deposits/{dep1}/files/..%2Fescape.txt", b"x"),
    U("/deposits/{dep1}/files/" + "n" * 300 + ".txt", b"x"),
    U("/deposits/{dep1}/files/declared.txt", b"12345",
      headers={"x-checksum-sha256": "0" * 64}),
    U("/deposits/{dep1}/files/declared2.txt", b"12345", headers={"content-length": "5"}),
    U("/deposits/nope/files/a.csv", b"x"),
    G("/deposits/{dep1}"),
    X("/deposits/{dep1}/files/empty.txt"), X("/deposits/{dep1}/files/empty.txt"),
    X("/deposits/{dep1}/files/never-there.txt"), X("/deposits/nope/files/a.csv"),
    X("/deposits/{dep1}/files/dataset.x.json"),
    G("/deposits/{dep1}/summary"),
    P("/deposits/{dep1}/finalise"), P("/deposits/{dep1}/finalise"),
    G("/deposits/{dep1}"), G("/deposits/{dep1}/summary"),
    U("/deposits/{dep1}/files/late.csv", b"x"), X("/deposits/{dep1}/files/one.csv"),
    P("/deposits/nope/finalise"),
    P("/deposits", json=dict(FORM, dataset="empty-one")),              # -> {dep2}
    P("/deposits/{dep2}/finalise"),
    P("/deposits", json=dict(FORM, dataset="third")),                  # -> {dep3}
    P("/deposits", json=dict(FORM, dataset="fourth")),
    P("/deposits", json=dict(FORM, dataset="fifth")),
    P("/deposits", json=dict(FORM, dataset="sixth")),
    P("/deposits", json=dict(FORM, dataset="seventh")),
    U("/deposits/{dep3}/files/a.txt", b"x" * 30), U("/deposits/{dep3}/files/b.txt", b"y" * 30),
    U("/deposits/{dep3}/files/c.txt", b"z" * 30), U("/deposits/{dep3}/files/d.txt", b"w" * 50),
    P("/deposits/{dep3}/finalise"),
    # forms that should be refused, each for one reason
    P("/deposits", json=dict(FORM, sensitivity="red")),
    P("/deposits", json=dict(FORM, strand="rs9")), P("/deposits", json=dict(FORM, strand="centre")),
    P("/deposits", json=dict(FORM, state="final")), P("/deposits", json=dict(FORM, project="CSAC")),
    P("/deposits", json=dict(FORM, dataset="Bad Slug")), P("/deposits", json=dict(FORM, domain="nope")),
    P("/deposits", json=dict(FORM, domain=["quant", "geo"])),
    P("/deposits", json=dict(FORM, domain=["quant", "quant"])),
    P("/deposits", json=dict(FORM, subject=["no-such-term"])), P("/deposits", json=dict(FORM, subject=[])),
    P("/deposits", json=dict(FORM, subject="forced-labour")),
    P("/deposits", json=dict(FORM, subject=["child-soldiers"])),
    P("/deposits", json=dict(FORM, abstract="too short")), P("/deposits", json=dict(FORM, abstract="")),
    P("/deposits", json=dict(FORM, version="3")), P("/deposits", json=dict(FORM, version="v3.1")),
    P("/deposits", json=dict(FORM, coverage_start="2020-13-01")),
    P("/deposits", json=dict(FORM, coverage_start="2022", coverage_end="2021")),
    P("/deposits", json=dict(FORM, source_type="nope")),
    P("/deposits", json=dict(FORM, source_type="derived", dataset="d1")),
    P("/deposits", json=dict(FORM, dataset="d2", derived_from=GREEN + "\nhttps://e.org/x\nA book")),
    P("/deposits", json=dict(FORM, dataset="d3", derived_from=[{"kind": "dataset", "identifier": "x"}])),
    P("/deposits", json=dict(FORM, dataset="d4", provider=["nobody"])),
    P("/deposits", json=dict(FORM, dataset="d5", spatial=["iso:GB", "m49:999"])),
    P("/deposits", json=dict(FORM, dataset="d6", spatial="Somewhere", spatial_note="n")),
    P("/deposits", json=dict(FORM, dataset="d7", license="CC0", creator="A", notes="n",
                             language=["en"], ethics_ref="E-1", source_detail="An archive box")),
    P("/deposits", json=dict(FORM, dataset="d8", provenance=[{"activity": "nope"}])),
    P("/deposits", json=dict(FORM, dataset="d9", extra_field="x")),
    P("/deposits", json={k: v for k, v in FORM.items() if k != "strand"}),
    P("/deposits", json={}), P("/deposits", json=[]), P("/deposits", json="text"),
    P("/deposits", content=b"{not json", headers={"content-type": "application/json"}),
    P("/deposits", content=b"a=b", headers={"content-type": "application/x-www-form-urlencoded"}),
    # an existing dataset: refused without confirm_existing, then accepted
    P("/deposits", json=dict(FORM, dataset="events")),
    P("/deposits", json=dict(FORM, dataset="events", confirm_existing=True)),
    P("/deposits", json=dict(FORM, dataset="events", confirm_existing=True, version="0-1")),
    G("/"), G("/deposits/lookup?strand=rs2&project=csac&sensitivity=green&state=0_raw&dataset=compare"),
]

PROXY_EXTRA = [(m, p, dict(kw, headers=dict(kw.get("headers") or {}, **{"x-webauth-user": USER})))
               for m, p, kw in (SERVICE[:7] + FIND[:6] + DEPOSIT[:8])]


def script_for(name):
    if name.startswith("proxy"):
        return SERVICE + PROXY_EXTRA
    if name == "header diagnostic on":
        return SERVICE
    if name.startswith("search on"):
        return SERVICE[:7] + SEARCH + FIND[:4]
    if name == "read role off":
        return SERVICE + DEPOSIT + FIND[:3] + FIND[-5:] + SEARCH[:3]
    if name == "small limits":
        return DEPOSIT
    return SERVICE[:7] + FIND + DEPOSIT + SEARCH[:2]


# ------------------------------------------------------------ comparing

HEADERS = ("content-type", "location", "content-range", "content-length", "accept-ranges",
           "content-disposition", "cache-control", "set-cookie", "www-authenticate",
           "allow", "x-content-type-options")   # not etag: a static file's is made from its modified time
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)")
SEEDED = {"-".join((c * 8, c * 4, "4" + c * 3, "8" + c * 3, c * 12))
          for c in "12345"}


class Side:
    def __init__(self, which, client):
        self.which, self.client = which, client
        self.deps, self.uuids = [], {}

    def send(self, method, path, kw):
        try:
            path = path.format(**{"dep%d" % (i + 1): d for i, d in enumerate(self.deps)})
        except (KeyError, IndexError):
            return ("skipped: an earlier request did not give a deposit id",)
        kw = dict(kw)
        cookies = kw.pop("cookies", None)
        if cookies:
            kw["headers"] = dict(kw.get("headers") or {},
                                 cookie="; ".join("%s=%s" % c for c in cookies.items()))
        r = self.client.request(method, path, **kw)
        if method == "POST" and path == "/deposits" and r.status_code == 201:
            self.deps.append(r.json()["id"])
        body = r.content.decode("utf-8", "replace")
        headers = {h: r.headers[h] for h in HEADERS if h in r.headers}
        return (r.status_code, self.clean(json.dumps(headers, sort_keys=True)), self.clean(body))

    def clean(self, text):
        text = text.replace(BUCKET[self.which], "<bucket>")
        for i, dep in enumerate(self.deps):
            text = text.replace(dep, "<dep%d>" % (i + 1))

        def uuid(m):
            if m.group(0) in SEEDED:
                return m.group(0)
            return self.uuids.setdefault(m.group(0), "<uuid%d>" % (len(self.uuids) + 1))

        return TIME_RE.sub("<time>", UUID_RE.sub(uuid, text))


def main():
    env = {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
           "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": "",
           # The unpacked copy has no git folder to describe itself from.
           "CRSW_VERSION": "compare"}
    total = differ = 0
    statuses = {}
    paths = set()
    for name, extra, opts in CONFIGS:
        # A fresh store for every configuration, so each starts the same.
        with mock.patch.dict(os.environ, env), mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1",
                              aws_access_key_id="t", aws_secret_access_key="t")
            sides = []
            for which in ("old", "new"):
                seed(s3, BUCKET[which])
                sides.append(Side(which, build(which, s3, extra, opts)))
            n = d = 0
            for method, path, kw in script_for(name):
                a, b = (side.send(method, path, kw) for side in sides)
                n += 1
                statuses[a[0]] = statuses.get(a[0], 0) + 1
                paths.add((method, re.sub(r"\?.*", "", path)))
                if a != b:
                    d += 1
                    if differ + d <= SHOW:
                        print("DIFFERENT [%s] %s %s" % (name, method, path))
                        for label, x, y in zip(("status", "headers", "body"), a, b):
                            if x != y:
                                x, y = str(x), str(y)
                                i = next((k for k in range(min(len(x), len(y)))
                                          if x[k] != y[k]), min(len(x), len(y)))
                                print("  %s differs at %d:\n    old: %r\n    new: %r"
                                      % (label, i, x[max(0, i - 60):i + 120],
                                         y[max(0, i - 60):i + 120]))
            print("%-42s %4d requests, %d differ" % (name, n, d))
            total += n
            differ += d
    print("statuses seen: %s" % ", ".join(
        "%s x%d" % (k, v) for k, v in sorted(statuses.items(), key=lambda kv: str(kv[0]))))
    print("web app: %d requests over %d configurations, %d distinct method+path, %d differ"
          % (total, len(CONFIGS), len(paths), differ))
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
