"""Searching inside documents, the promoter side: text out of files,
passages, embeddings under index/passages/, incremental by checksum,
caps, exclusions, sensitivities, and the after-run hook. The platform is
a stub on the client's `embed`; nothing talks HTTP."""
import io
import json
import os
import unittest
from unittest import mock

from tests.test_promoter import (HAVE_WEB, PromoterBase, FORM, DEST, VOCAB)

if HAVE_WEB:
    import docx
    import pyarrow.parquet as pq
    from crsw_web import llm
    from promoter import __main__ as cli
    from promoter import passages as pmod
    from promoter.config import ConfigError, PromoterConfig

LLM_ENV = {"PROMOTER_LLM_BASE_URL": "https://ai.example/api/v1",
           "PROMOTER_LLM_API_KEY": "sk-test", "PROMOTER_PASSAGES": "1"}
KEY = "index/passages/" + DEST + ".parquet"
FP = "arc:embedvl|Fake/Model|FP16|1"      # the stubbed fingerprint of the default alias


def make_pdf(pages):
    """A small but real PDF, one page per text; pypdf reads the text back."""
    objs = []                       # (object number, body bytes)
    kids = []
    n = 3                           # 1 catalog, 2 pages tree, 3 font
    for text in pages:
        content = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.replace("(", "").replace(")", "")).encode()
        n += 1; page_no = n
        n += 1; stream_no = n
        objs.append((page_no, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                              b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % stream_no))
        objs.append((stream_no, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"))
        kids.append(page_no)
    head = [(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
            (2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))),
            (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")]
    out = io.BytesIO(); out.write(b"%PDF-1.4\n")
    offsets = {}
    for num, body in head + objs:
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n" % num + body + b"\nendobj\n")
    xref = out.tell()
    count = max(offsets) + 1
    out.write(b"xref\n0 %d\n" % count + b"0000000000 65535 f \n")
    for i in range(1, count):
        out.write(b"%010d 00000 n \n" % offsets[i])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (count, xref))
    return out.getvalue()


def make_docx(paragraphs):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO(); d.save(buf)
    return buf.getvalue()


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestExtractAndChunk(unittest.TestCase):
    def test_types(self):
        self.assertEqual(pmod.extract("a.txt", b"hello  world\n"), [(None, "hello  world\n")])
        self.assertEqual(pmod.extract("a.TXT", b"   "), [])
        self.assertIsNone(pmod.extract("a.csv", b"a,b\n1,2\n"))
        self.assertIsNone(pmod.extract("a.png", b"\x89PNG"))
        pages = pmod.extract("t.pdf", make_pdf(["First page words", "Second page words"]))
        self.assertEqual([p for p, _ in pages], [1, 2])
        self.assertIn("First page words", pages[0][1])
        self.assertEqual(pmod.extract("empty.pdf", make_pdf([""])), [])   # image-only-like
        pages = pmod.extract("w.docx", make_docx(["One paragraph.", "", "Two."]))
        self.assertEqual(pages, [(None, "One paragraph.\nTwo.")])

    def test_chunk_overlap_and_page(self):
        words = ["w%d" % i for i in range(1000)]
        pages = [(1, " ".join(words[:400])), (2, " ".join(words[400:]))]
        pieces = pmod.chunk(pages, words=350, overlap=50)
        self.assertEqual([(p, pos) for p, pos, _ in pieces], [(1, 0), (1, 1), (2, 2), (2, 3)])
        self.assertEqual(pieces[0][2].split()[:2], ["w0", "w1"])
        self.assertEqual(pieces[1][2].split()[0], "w300")           # 350 - 50 overlap
        self.assertEqual(pieces[-1][2].split()[-1], "w999")
        # The defaults: 150 words, each starting 30 before the last ended
        # (chosen by the probe, docs/web-deposit/passage-length-handoff.md).
        self.assertEqual((pmod.PASSAGE_WORDS, pmod.OVERLAP_WORDS), (150, 30))
        pieces = pmod.chunk(pages)
        self.assertEqual(len(pieces[0][2].split()), 150)
        self.assertEqual(pieces[1][2].split()[0], "w120")
        self.assertEqual(pmod.chunk([(None, "   ")]), [])
        self.assertEqual(pmod.chunk([(None, "a  b\n c")]), [(None, 0, "a b c")])

    def test_excluded(self):
        self.assertTrue(pmod.excluded("rs1/oral/amber/1_interim/x", ("rs1/oral",)))
        self.assertTrue(pmod.excluded("rs1/oral/amber/1_interim/x", ("rs1/oral/amber/1_interim/x",)))
        self.assertFalse(pmod.excluded("rs1/oralhistory/amber/1_interim/x", ("rs1/oral",)))
        self.assertFalse(pmod.excluded("rs1/oral/amber/1_interim/x", ("",)))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestCap(unittest.TestCase):
    """The per-dataset cap, walked by `build` directly."""

    class Platform:
        def __init__(self):
            self.sent = []

        def embed(self, texts, batch=32):
            self.sent.extend(texts)
            return [[float(len(t)), 0.5] for t in texts]

    def words(self, n, tag):
        return " ".join("%s%d" % (tag, i) for i in range(n)).encode()

    def build(self, files, existing, cap):
        import hashlib
        rec = {"dataset_uuid": "u", "sensitivity": "green",
               "files": [{"path": m, "checksum_sha256": hashlib.sha256(d).hexdigest(),
                          "bytes": len(d)} for m, d in files]}
        data = dict(files)
        self.fetched, self.platform = [], self.Platform()

        def fetch(member):
            self.fetched.append(member)
            return data[member]
        # The cap arithmetic in this class was written for 350-word passages
        # with a 50-word overlap (1000 words = four passages); the size is
        # not what these tests are about, so they say so explicitly.
        return pmod.build("rs1/p/green/0_raw/d", rec, fetch, existing, self.platform,
                          "toy", None, 10 ** 9, cap, words=350, overlap=50)

    def build_default(self, files, existing):
        """`build` at the default passage size."""
        import hashlib
        rec = {"dataset_uuid": "u", "sensitivity": "green",
               "files": [{"path": m, "checksum_sha256": hashlib.sha256(d).hexdigest(),
                          "bytes": len(d)} for m, d in files]}
        data = dict(files)
        self.platform = self.Platform()
        return pmod.build("rs1/p/green/0_raw/d", rec, lambda m: data[m], existing,
                          self.platform, "toy", None, 10 ** 9, 100)

    def test_rows_say_how_they_were_split(self):
        a, one = self.words(1000, "a"), self.words(20, "o")
        rows, s = self.build_default([("a.txt", a), ("one.txt", one)], [])
        self.assertEqual(s.split, "150/30")
        self.assertEqual({r["split"] for r in rows}, {"150/30"})
        self.assertEqual([len(r["text"].split()) for r in rows][:2], [150, 150])
        self.assertEqual(rows[1]["text"].split()[0], "a120")
        self.assertEqual(s.passages, 10)                        # nine of a, one of one
        back = pq.read_table(io.BytesIO(pmod.parquet_bytes(rows))).to_pylist()
        self.assertEqual({r["split"] for r in back}, {"150/30"})
        # Unchanged files under the same layout are kept as before.
        rows, s = self.build_default([("a.txt", a), ("one.txt", one)], rows)
        self.assertEqual((s.files_kept, s.embedded, s.kept), (2, 0, 10))
        # Files cut under another layout are read and split again although
        # checksum, model and cut all match. A passage whose text is the
        # same under both layouts keeps its vector: one.txt is one passage
        # either way, so only its split changes.
        old, _ = self.build([("a.txt", a), ("one.txt", one)], [], 100)      # 350/50
        self.assertEqual({r["split"] for r in old}, {"350/50"})
        rows, s = self.build_default([("a.txt", a), ("one.txt", one)], old)
        self.assertEqual((s.files_read, s.files_kept, s.embedded, s.kept), (2, 0, 9, 1))
        self.assertEqual({r["split"] for r in rows}, {"150/30"})
        # Rows written before the column existed (every file in the bucket
        # on 16 October 2026) count as another layout too.
        before = [{k: v for k, v in r.items() if k != "split"} for r in rows]
        rows, s = self.build_default([("a.txt", a), ("one.txt", one)], before)
        self.assertEqual((s.files_read, s.files_kept, s.embedded, s.kept), (2, 0, 0, 10))
        self.assertEqual({r["split"] for r in rows}, {"150/30"})

    def members(self, rows):
        return [r["member"] for r in rows if r["position"] != pmod.NO_TEXT]

    def test_the_cap_holds_and_the_walk_stops_there(self):
        a, b, c = self.words(1000, "a"), self.words(1000, "b"), self.words(10, "c")
        rows, s = self.build([("a.txt", a), ("b.txt", b), ("c.txt", c)], [], 6)
        self.assertEqual(self.members(rows), ["a.txt"] * 4 + ["b.txt"] * 2)
        self.assertEqual(self.fetched, ["a.txt", "b.txt"])          # c never fetched
        self.assertTrue(s.capped)
        # a grows to five passages: b, cut short before, is kept but only
        # as far as the room left, and c is still not fetched.
        grown = self.words(1300, "a")
        rows, s = self.build([("a.txt", grown), ("b.txt", b), ("c.txt", c)], rows, 6)
        self.assertEqual(self.members(rows), ["a.txt"] * 5 + ["b.txt"])
        self.assertTrue(all(r["partial"] for r in rows if r["member"] == "b.txt"))
        self.assertEqual(self.fetched, ["a.txt"])
        self.assertEqual(s.passages, 6)
        # A whole file kept is trimmed the same way when the cap is lowered.
        rows, s = self.build([("a.txt", grown), ("b.txt", b), ("c.txt", c)], rows, 3)
        self.assertEqual(self.members(rows), ["a.txt"] * 3)
        self.assertEqual(self.fetched, [])
        self.assertTrue(s.capped)

    def test_passages_cut_by_the_cap_are_not_counted_as_dropped(self):
        a, b = self.words(1000, "a"), self.words(1000, "b")
        rows, _ = self.build([("a.txt", a), ("b.txt", b)], [], 100)
        rows, s = self.build([("a.txt", a), ("b.txt", b)], rows, 4)
        self.assertEqual((s.passages, s.capped, s.dropped), (4, True, 0))
        rows, s = self.build([("a.txt", a)], rows, 100)        # b leaves the record
        self.assertEqual((s.passages, s.capped, s.dropped), (4, False, 0))
        rows, _ = self.build([("a.txt", a), ("b.txt", b)], [], 100)
        rows, s = self.build([("a.txt", a)], rows, 100)
        self.assertEqual(s.dropped, 4)

    def test_dropped_counts_rows_left_out_but_not_files_beyond_the_cap(self):
        a, b, c = self.words(1000, "a"), self.words(1000, "b"), self.words(1000, "c")
        rows, _ = self.build([("a.txt", a), ("b.txt", b), ("c.txt", c)], [], 100)
        # b is cut mid-file and c is never reached: neither is "dropped".
        rows, s = self.build([("a.txt", a), ("b.txt", b), ("c.txt", c)], rows, 6)
        self.assertEqual((s.passages, s.capped, s.dropped), (6, True, 0))
        # A file still in the record but now over the size cap loses its
        # rows, and the summary says so.
        short = self.words(500, "b")                             # two passages
        rows, _ = self.build([("a.txt", a), ("b.txt", short)], [], 100)
        big = self.build_with_limit([("a.txt", a), ("b.txt", short)], rows, 100,
                                    max_file_bytes=len(a) - 1)
        self.assertEqual((big.passages, big.files_too_big, big.dropped), (2, 1, 4))

    def build_with_limit(self, files, existing, cap, max_file_bytes):
        import hashlib
        rec = {"dataset_uuid": "u", "sensitivity": "green",
               "files": [{"path": m, "checksum_sha256": hashlib.sha256(d).hexdigest(),
                          "bytes": len(d)} for m, d in files]}
        data = dict(files)
        _, s = pmod.build("rs1/p/green/0_raw/d", rec, lambda m: data[m], existing,
                          self.Platform(), "toy", None, max_file_bytes, cap,
                          words=350, overlap=50)
        return s

    def test_a_reused_passage_takes_the_record_as_it_is_now(self):
        import hashlib
        a = self.words(1000, "a")
        rows, _ = self.build([("a.txt", a)], [], 100)
        longer = a + b" " + self.words(10, "z")
        rec = {"dataset_uuid": "new-uuid", "sensitivity": "amber",
               "files": [{"path": "a.txt", "checksum_sha256": hashlib.sha256(longer).hexdigest(),
                          "bytes": len(longer)}]}
        rows, s = pmod.build("rs1/p/amber/0_raw/d", rec, lambda m: longer, rows,
                             self.Platform(), "toy", None, 10 ** 9, 100, words=350, overlap=50)
        self.assertEqual(s.kept, 3)
        self.assertEqual({(r["identifier"], r["dataset_uuid"], r["sensitivity"]) for r in rows},
                         {("rs1/p/amber/0_raw/d", "new-uuid", "amber")})

    def test_a_changed_file_reuses_the_vectors_of_unchanged_passages(self):
        a = self.words(1000, "a")
        rows, _ = self.build([("a.txt", a)], [], 100)
        longer = a + b" " + self.words(10, "z")                # the last passage changes
        rows, s = self.build([("a.txt", longer)], rows, 100)
        self.assertEqual((s.files_read, s.embedded, s.kept, s.passages), (1, 1, 3, 4))
        self.assertEqual(self.platform.sent[0][:3], "a90")
        import hashlib
        self.assertEqual({r["checksum"] for r in rows}, {hashlib.sha256(longer).hexdigest()})
        self.assertTrue(all(r["embedding"] for r in rows))

    def test_finishing_a_cut_file_embeds_only_what_is_new(self):
        a, b = self.words(1000, "a"), self.words(1000, "b")
        rows, _ = self.build([("a.txt", a), ("b.txt", b)], [], 6)
        rows, s = self.build([("a.txt", a), ("b.txt", b)], rows, 100)
        self.assertEqual(self.members(rows), ["a.txt"] * 4 + ["b.txt"] * 4)
        self.assertEqual((s.embedded, s.kept, s.capped), (2, 6, False))
        self.assertEqual(len(self.platform.sent), 2)
        self.assertFalse(any(r["partial"] for r in rows))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class PassagesBase(PromoterBase):
    ENV = {"PROMOTER_S3_ENDPOINT": "https://rgw.example", "PROMOTER_S3_ACCESS_KEY": "t",
           "PROMOTER_S3_SECRET_KEY": "t", "PROMOTER_S3_BUCKET": "crsw",
           "PROMOTER_STAGING_PREFIX": "staging/_test", "PROMOTER_LOG_PATH": "",
           "PROMOTER_AUDIT_PREFIX": ""}
    FILES = (("notes.txt", b"plain words here\n"),
             ("paper.pdf", None),            # filled in setUp
             ("memo.docx", None),
             ("table.csv", b"a,b\n1,2\n"),
             ("scan.pdf", None))

    def setUp(self):
        super().setUp()
        self.sent = []

        def fake_embed(platform, texts, batch=32):
            self.sent.extend(texts)
            return [[float(len(t)), 0.5] for t in texts]
        p = mock.patch.object(llm.Platform, "embed", fake_embed)
        p.start(); self.addCleanup(p.stop)

        def fake_fingerprint(platform):
            return platform.embed_model + "|Fake/Model|FP16|1"
        fp = mock.patch.object(llm.Platform, "embed_fingerprint", fake_fingerprint)
        fp.start(); self.addCleanup(fp.stop)
        self.files = [("notes.txt", b"plain words here\n"),
                      ("paper.pdf", make_pdf(["Alpha page text", "Beta page text"])),
                      ("memo.docx", make_docx(["Memo paragraph one.", "Memo paragraph two."])),
                      ("table.csv", b"a,b\n1,2\n"),
                      ("scan.pdf", make_pdf([""]))]

    def cli(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(self.ENV, **(env or {}))), \
             mock.patch("promoter.__main__.s3mod.make_client", return_value=self.s3), \
             mock.patch("promoter.__main__.vocab.load_vocabulary", return_value=(VOCAB, "bundled")), \
             mock.patch("builtins.print") as printed:
            code = cli.main(argv + ["--env-file", os.devnull])
        return code, [c.args[0] for c in printed.call_args_list if c.args]

    def rows(self, key=KEY):
        body = self.s3.get_object(Bucket="crsw", Key=key)["Body"].read()
        return pq.read_table(io.BytesIO(body)).to_pylist()

    def written(self, out, action="passages_written"):
        return [json.loads(o) for o in out if '"%s"' % action in o]


class TestPassages(PassagesBase):
    def test_off_by_default_and_refusals(self):
        self.stage(files=self.files); self.cli(["run"], env={"PROMOTER_LLM_BASE_URL": "https://ai.example",
                                                           "PROMOTER_LLM_API_KEY": "k"})
        self.assertEqual(self.keys_under("index/passages/"), [])
        code, out = self.cli(["passages"])
        self.assertEqual(code, 2)
        code, out = self.cli(["passages"], env={"PROMOTER_PASSAGES": "1"})   # no platform
        self.assertEqual(code, 2)
        cfg = PromoterConfig.from_env(dict(self.ENV, **LLM_ENV,
                                           PROMOTER_PASSAGES_EXCLUDE=" rs1/oral/, rs2/x "))
        self.assertTrue(cfg.passages_enabled)
        self.assertEqual(cfg.passages_exclude, ("rs1/oral", "rs2/x"))
        self.assertEqual((cfg.passages_max_file_bytes, cfg.passages_max_per_dataset),
                         (50 * 1024 * 1024, 20000))

    def test_a_blank_index_prefix_turns_passages_off(self):
        env = dict(LLM_ENV, PROMOTER_INDEX_PREFIX="")
        self.assertFalse(PromoterConfig.from_env(dict(self.ENV, **env)).passages_enabled)
        self.stage(files=self.files)
        code, out = self.cli(["run"], env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.written(out), [])
        self.assertEqual([k for k in self.keys_under("") if "passages" in k], [])
        code, out = self.cli(["passages"], env=env)
        self.assertEqual(code, 2)

    def test_run_writes_passages_from_text_files_only(self):
        self.stage(files=self.files)
        code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        actions = [json.loads(o)["action"] for o in out]
        # Each step that embeds says what it embedded with.
        self.assertEqual(actions[-6:], ["index_written", "embed_model_resolved",
                                        "embeddings_written", "embed_model_resolved",
                                        "passages_written", "finish"])
        s = self.written(out)[0]
        self.assertEqual((s["identifier"], s["files"], s["files_read"], s["files_other"],
                          s["files_no_text"], s["passages"], s["embedded"], s["kept"],
                          s["dimension"], s["key"], s["capped"]),
                         (DEST, 5, 4, 1, 1, 3, 3, 0, 2, KEY, False))
        rows = self.rows()
        self.assertEqual([(r["member"], r["page"], r["position"]) for r in rows],
                         [("memo.docx", None, 0), ("notes.txt", None, 0), ("paper.pdf", 1, 0),
                          ("scan.pdf", None, pmod.NO_TEXT)])
        by = {r["member"]: r for r in rows}
        self.assertIsNone(by["scan.pdf"]["embedding"])      # examined, no text: remembered
        self.assertIsNone(by["scan.pdf"]["text"])
        self.assertEqual(by["notes.txt"]["text"], "plain words here")
        self.assertIn("Alpha page text", by["paper.pdf"]["text"])
        self.assertIn("Beta page text", by["paper.pdf"]["text"])
        self.assertEqual(by["memo.docx"]["text"], "Memo paragraph one. Memo paragraph two.")
        self.assertEqual((by["notes.txt"]["words"], by["notes.txt"]["model"],
                          by["notes.txt"]["cut"], by["notes.txt"]["sensitivity"],
                          by["notes.txt"]["split"]),
                         (3, FP, 1024, "green", "150/30"))
        self.assertEqual(s["split"], "150/30")
        self.assertEqual(len(by["notes.txt"]["checksum"]), 64)
        self.assertAlmostEqual(by["notes.txt"]["embedding"][1], 0.5, places=3)
        # What went to the platform: the dataset's own text (embeddings.parquet)
        # and the three passages. Never the CSV, never a file name.
        self.assertEqual(len(self.sent), 4)
        self.assertFalse(any("a,b" in t or ".csv" in t for t in self.sent))

    def test_a_changed_fingerprint_re_reads_every_file(self):
        """The alias unchanged, the backend behind it swapped: every
        passage is embedded again, and the audit line says with what."""
        self.stage(files=self.files)
        self.cli(["run"], env=LLM_ENV)
        new = "arc:embedvl|New/Model|FP8|2"
        self.sent.clear()
        with mock.patch.object(llm.Platform, "embed_fingerprint", lambda p: new):
            code, out = self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        s = self.written(out)[0]
        self.assertEqual((s["embedded"], s["kept"]), (3, 0))
        self.assertEqual({r["model"] for r in self.rows()}, {new})
        self.assertEqual(self.written(out, "embed_model_resolved")[0]["model"], new)
        self.assertEqual(len(self.sent), 3)
        # The same fingerprint again: every passage kept, nothing sent.
        self.sent.clear()
        with mock.patch.object(llm.Platform, "embed_fingerprint", lambda p: new):
            code, out = self.cli(["passages"], env=LLM_ENV)
        s = self.written(out)[0]
        self.assertEqual((s["embedded"], s["kept"]), (0, 3))
        self.assertEqual(self.sent, [])

    def test_a_model_change_sweeps_every_passages_file(self):
        """The backend behind the alias moved: the run rebuilds the
        passages of every dataset in place, not only what it promoted."""
        second = DEST.rsplit("/", 1)[0] + "/second"
        self.stage(files=self.files); self.cli(["run"], env=LLM_ENV)
        self.stage(files=self.files, dataset="second"); self.cli(["run"], env=LLM_ENV)
        self.assertEqual(len(self.keys_under("index/passages/")), 2)
        new = "arc:embedvl|New/Model|FP8|2"
        self.sent.clear()
        with mock.patch.object(llm.Platform, "embed_fingerprint", lambda p: new):
            code, out = self.cli(["run"], env=LLM_ENV)          # nothing staged
        self.assertEqual(code, 0, out)
        actions = [json.loads(o)["action"] for o in out]
        self.assertIn("embed_model_changed", actions)
        written = self.written(out)
        self.assertEqual(sorted(w["identifier"] for w in written), sorted([DEST, second]))
        self.assertEqual([(w["embedded"], w["kept"]) for w in written], [(3, 0), (3, 0)])
        self.assertEqual({r["model"] for r in self.rows()}, {new})
        self.assertEqual({r["model"] for r in self.rows("index/passages/" + second + ".parquet")},
                         {new})
        # Dataset vectors (2) and passages (6): nothing else went out.
        self.assertEqual(len(self.sent), 8)

    def test_incremental_by_checksum_and_drop(self):
        self.stage(files=self.files); self.cli(["run"], env=LLM_ENV)
        self.sent.clear()
        code, out = self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        s = self.written(out)[0]
        self.assertEqual((s["files_kept"], s["kept"], s["embedded"], s["files_read"],
                          s["files_no_text"]), (4, 3, 0, 0, 1))
        self.assertEqual(self.sent, [])
        # One file changes and one is added: only those are read and embedded.
        self.stage(files=[("notes.txt", b"different words now\n"), ("extra.md", b"# More\ntext\n")])
        self.sent.clear()
        code, out = self.cli(["run"], env=LLM_ENV)
        s = self.written(out)[0]
        self.assertEqual((s["files_kept"], s["files_read"], s["embedded"], s["kept"], s["passages"]),
                         (3, 2, 2, 2, 4))
        # (The dataset's own text may be re-sent too, if the record's modified
        # stamp moved on; only the passages are of interest here.)
        self.assertEqual(sorted(t for t in self.sent if not t.startswith("promo\n")),
                         ["# More text", "different words now"])
        # A different cut re-embeds everything; nothing is dropped until a
        # member leaves the record (which deposits never do).
        self.sent.clear()
        code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_LLM_EMBED_DIMS="512"))
        s = self.written(out)[0]
        self.assertEqual((s["embedded"], s["kept"], s["dropped"]), (4, 0, 0))
        self.assertEqual({r["cut"] for r in self.rows()}, {512})

    def test_sensitivity_exclusion_and_caps(self):
        self.stage(files=self.files, sensitivity="amber")
        amber = DEST.replace("/green/", "/amber/")
        code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        skipped = self.written(out, "passages_skipped")
        self.assertEqual(skipped[0]["prefix"], amber)
        self.assertIn("PROMOTER_LLM_SENSITIVITIES", skipped[0]["reason"])
        self.assertEqual(self.keys_under("index/passages/"), [])
        code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_LLM_SENSITIVITIES="green,amber",
                                                    PROMOTER_PASSAGES_EXCLUDE="rs2/csac/amber"))
        self.assertIn("PROMOTER_PASSAGES_EXCLUDE", self.written(out, "passages_skipped")[0]["reason"])
        self.assertEqual(self.keys_under("index/passages/"), [])
        code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_LLM_SENSITIVITIES="green,amber",
                                                    PROMOTER_PASSAGES_MAX_FILE_BYTES="2000",
                                                    PROMOTER_PASSAGES_MAX_PER_DATASET="1"))
        s = self.written(out)[0]                     # memo.docx is over 2000 bytes
        self.assertEqual((s["files_too_big"], s["passages"], s["capped"]), (1, 1, True))
        self.assertEqual(self.rows("index/passages/" + amber + ".parquet")[0]["member"], "notes.txt")
        # The command's own summary line, and --dataset / --strand selection.
        finished = self.written(out, "passages_finished")[0]
        self.assertEqual((finished["failed"], finished["datasets"], finished["passages"]), (0, 1, 1))
        code, out = self.cli(["passages", "--strand", "rs1"], env=LLM_ENV)
        self.assertEqual(self.written(out, "passages_finished")[0]["datasets"], 0)

    def test_a_file_cut_by_the_cap_is_finished_when_the_cap_is_raised(self):
        long_text = " ".join("word%d" % i for i in range(1000)).encode()   # four passages at 350/50
        sized = dict(LLM_ENV, PROMOTER_PASSAGE_WORDS="350", PROMOTER_PASSAGE_OVERLAP="50")
        self.stage(files=[("long.txt", long_text), ("notes.txt", b"plain words here\n")])
        capped = dict(sized, PROMOTER_PASSAGES_MAX_PER_DATASET="2")
        self.cli(["run"], env=capped)
        self.assertEqual([r["member"] for r in self.rows()], ["long.txt", "long.txt"])
        # The same cap again: the cut file is kept as it is, not re-embedded.
        self.sent.clear()
        code, out = self.cli(["passages"], env=capped)
        s = self.written(out)[0]
        self.assertEqual((s["embedded"], s["kept"], s["capped"]), (0, 2, True))
        self.assertEqual(self.sent, [])
        # The cap raised: the rest of the cut file is read, and the next file.
        code, out = self.cli(["passages"], env=sized)
        s = self.written(out)[0]
        self.assertEqual((s["passages"], s["capped"]), (5, False))
        self.assertEqual([r["member"] for r in self.rows()], ["long.txt"] * 4 + ["notes.txt"])
        # And once whole, it is kept like any other file.
        self.sent.clear()
        code, out = self.cli(["passages"], env=sized)
        self.assertEqual(self.written(out)[0]["files_kept"], 2)
        self.assertEqual(self.sent, [])

    def test_the_passage_size_is_a_setting(self):
        cfg = PromoterConfig.from_env(dict(self.ENV, **LLM_ENV))
        self.assertEqual((cfg.passage_words, cfg.passage_overlap), (150, 30))
        sized = dict(LLM_ENV, PROMOTER_PASSAGE_WORDS="40", PROMOTER_PASSAGE_OVERLAP="10")
        cfg = PromoterConfig.from_env(dict(self.ENV, **sized))
        self.assertEqual((cfg.passage_words, cfg.passage_overlap), (40, 10))
        for bad in ({"PROMOTER_PASSAGE_OVERLAP": "150"}, {"PROMOTER_PASSAGE_OVERLAP": "-1"},
                    {"PROMOTER_PASSAGE_WORDS": "5"}):
            with self.assertRaises(ConfigError) as cm:
                PromoterConfig.from_env(dict(self.ENV, **LLM_ENV, **bad))
            self.assertIn(list(bad)[0], str(cm.exception))
        # The run splits at the configured size and the rows say so.
        text = " ".join("word%d" % i for i in range(100)).encode()
        self.stage(files=[("long.txt", text)])
        code, out = self.cli(["run"], env=sized)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.written(out)[0]["split"], "40/10")
        rows = self.rows()
        self.assertEqual([(r["words"], r["split"]) for r in rows], [(40, "40/10")] * 3)
        self.assertEqual(rows[1]["text"].split()[0], "word30")

    def test_passages_no_longer_permitted_are_removed(self):
        self.stage(files=self.files)
        self.cli(["run"], env=LLM_ENV)
        self.assertEqual(self.keys_under("index/passages/"), [KEY])
        # Excluded after the passages were built: the old file goes.
        code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_PASSAGES_EXCLUDE="rs2/csac"))
        self.assertEqual(code, 0, out)
        removed = self.written(out, "passages_removed")
        self.assertEqual([(r["prefix"], r["key"]) for r in removed], [(DEST, KEY)])
        self.assertIn("PROMOTER_PASSAGES_EXCLUDE", removed[0]["reason"])
        self.assertEqual(self.keys_under("index/passages/"), [])
        # Built again, then its sensitivity taken off the list, on a promotion.
        self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(self.keys_under("index/passages/"), [KEY])
        self.stage(files=[("extra.md", b"# More\ntext\n")])
        code, out = self.cli(["run"], env=dict(LLM_ENV, PROMOTER_LLM_SENSITIVITIES="amber"))
        self.assertEqual(code, 0, out)
        self.assertIn("PROMOTER_LLM_SENSITIVITIES",
                      self.written(out, "passages_removed")[0]["reason"])
        self.assertEqual(self.keys_under("index/passages/"), [])
        # Nothing there to remove: skipped, no removal line.
        code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_PASSAGES_EXCLUDE="rs2/csac"))
        self.assertEqual(self.written(out, "passages_removed"), [])
        self.assertEqual(len(self.written(out, "passages_skipped")), 1)

    def test_passages_command_sweeps_with_one_listing(self):
        self.stage(files=self.files)
        self.cli(["run"], env=LLM_ENV)
        body = self.s3.get_object(Bucket="crsw", Key=KEY)["Body"].read()
        # A passages file whose dataset has no record in place any more.
        orphan = "index/passages/rs2/gone/green/0_raw/old.parquet"
        self.s3.put_object(Bucket="crsw", Key=orphan, Body=body)
        # Not permitted datasets are checked against the listing, not one by one.
        with mock.patch.object(self.s3, "head_object",
                               side_effect=AssertionError("one request per dataset")):
            code, out = self.cli(["passages"], env=dict(LLM_ENV, PROMOTER_PASSAGES_EXCLUDE="rs2"))
        self.assertEqual(code, 0, out)
        removed = {r["key"]: r["reason"] for r in self.written(out, "passages_removed")}
        self.assertEqual(set(removed), {KEY, orphan})
        self.assertIn("no record in place", removed[orphan])
        self.assertEqual(self.keys_under("index/passages/"), [])
        self.assertEqual(self.written(out, "passages_finished")[0]["removed"], 2)
        # Scoped runs leave files outside their scope alone.
        self.s3.put_object(Bucket="crsw", Key=orphan, Body=body)
        code, out = self.cli(["passages", "--strand", "rs1"], env=LLM_ENV)
        self.assertEqual(self.keys_under("index/passages/"), [orphan])
        code, out = self.cli(["passages", "--dataset", DEST], env=LLM_ENV)
        self.assertIn(orphan, self.keys_under("index/passages/"))

    def test_a_bad_strand_is_refused(self):
        code, out = self.cli(["passages", "--strand", "rs2/"], env=LLM_ENV)
        self.assertEqual(code, 2)
        self.assertEqual(self.written(out, "passages_finished"), [])

    def test_the_listing_is_scoped_and_its_failure_is_a_log_line(self):
        self.stage(files=self.files)
        self.cli(["run"], env=LLM_ENV)
        prefixes = []
        real = self.s3.get_paginator

        def paginator(name):
            pag = real(name)

            class Wrapped:
                def paginate(self, **kw):
                    if kw.get("Prefix", "").startswith("index/passages/"):
                        prefixes.append(kw["Prefix"])
                        if "boom" in prefixes[0]:
                            raise RuntimeError("listing refused")
                    return pag.paginate(**kw)
            return Wrapped()
        with mock.patch.object(self.s3, "get_paginator", paginator):
            self.cli(["passages", "--dataset", DEST], env=LLM_ENV)
            self.cli(["passages", "--strand", "rs2"], env=LLM_ENV)
            self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(prefixes, ["index/passages/" + DEST + ".parquet", "index/passages/rs2/",
                                    "index/passages/"])
        prefixes[:] = ["boom"]
        with mock.patch.object(self.s3, "get_paginator", paginator):
            code, out = self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(code, 1)
        self.assertIn("listing refused", self.written(out, "passages_failed")[0]["error"])
        self.assertEqual(self.written(out, "passages_finished")[0]["failed"], 1)
        self.assertEqual(self.written(out), [])           # nothing walked

    def test_failure_is_logged_and_the_run_still_succeeds(self):
        self.stage(files=self.files)
        with mock.patch.object(llm.Platform, "embed",
                               side_effect=llm.PlatformError("platform returned HTTP 503")):
            code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0)
        self.assertIn("503", self.written(out, "passages_failed")[0]["error"])
        self.assertEqual(self.keys_under("index/passages/"), [])
        with mock.patch.object(llm.Platform, "embed",
                               side_effect=llm.PlatformError("platform returned HTTP 503")):
            code, out = self.cli(["passages"], env=LLM_ENV)
        self.assertEqual(code, 1)
        self.assertEqual(self.written(out, "passages_finished")[0]["failed"], 1)
        # And a dataset with no text at all leaves no file behind.
        self.stage(files=[("only.csv", b"a\n")], dataset="tables")
        code, out = self.cli(["run"], env=LLM_ENV)
        s = self.written(out)[0]
        self.assertEqual((s["passages"], s["key"], s["files_other"]), (0, None, 1))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestOcr(unittest.TestCase):
    """A PDF with no text layer: its first pages rendered and read through
    the vision model (`ocr`), walked by `build` directly. The synthetic
    PDF's empty pages render white; the fake reader does not look."""

    class Platform:
        def __init__(self):
            self.sent = []

        def embed(self, texts, batch=32):
            self.sent.extend(texts)
            return [[float(len(t)), 0.5] for t in texts]

    def build(self, files, existing, ocr, ocr_pages=2):
        import hashlib
        rec = {"dataset_uuid": "u", "sensitivity": "green",
               "files": [{"path": m, "checksum_sha256": hashlib.sha256(d).hexdigest(),
                          "bytes": len(d)} for m, d in files]}
        data = dict(files)
        self.fetched, self.platform = [], self.Platform()

        def fetch(member):
            self.fetched.append(member)
            return data[member]
        return pmod.build("rs1/p/green/0_raw/d", rec, fetch, existing, self.platform,
                          "toy", None, 10 ** 9, 100, ocr=ocr, ocr_pages=ocr_pages)

    def reader(self):
        seen = []

        def ocr(image):
            seen.append(image)
            return "Scanned page %d words here" % len(seen)
        return seen, ocr

    def test_page_images_renders_the_first_pages_as_jpeg(self):
        images = pmod.page_images(make_pdf(["", "", ""]), 2)
        self.assertEqual(len(images), 2)
        for image in images:
            self.assertTrue(image.startswith(b"\xff\xd8"))      # JPEG
        self.assertEqual(len(pmod.page_images(make_pdf([""]), 5)), 1)
        self.assertEqual(pmod.page_images(b"not a pdf", 2), [])

    def test_the_first_pages_of_a_no_text_pdf_are_transcribed(self):
        seen, ocr = self.reader()
        files = [("scan.pdf", make_pdf(["", "", ""])), ("notes.txt", b"plain words")]
        rows, s = self.build(files, [], ocr)
        self.assertEqual(len(seen), 2)                      # two of the three pages
        self.assertTrue(all(i.startswith(b"\xff\xd8") for i in seen))
        mine = [r for r in rows if r["member"] == "scan.pdf"]
        self.assertEqual([(r["page"], r["position"], r["ocr"]) for r in mine], [(1, 0, 2)])
        self.assertEqual(mine[0]["text"],
                         "Scanned page 1 words here Scanned page 2 words here")
        self.assertIn(mine[0]["text"], self.platform.sent)
        self.assertNotIn(pmod.NO_TEXT, [r["position"] for r in rows])
        self.assertEqual([r["ocr"] for r in rows if r["member"] == "notes.txt"], [0])
        self.assertEqual((s.files_read, s.files_ocr, s.pages_ocr, s.files_no_text, s.passages),
                         (2, 1, 2, 0, 2))

    def test_off_leaves_the_no_text_row_as_before(self):
        rows, s = self.build([("scan.pdf", make_pdf(["", ""]))], [], None)
        self.assertEqual([(r["position"], r["ocr"]) for r in rows], [(pmod.NO_TEXT, 0)])
        self.assertEqual((s.files_ocr, s.pages_ocr, s.files_no_text), (0, 0, 1))

    def test_a_text_layer_never_reaches_the_model(self):
        seen, ocr = self.reader()
        rows, s = self.build([("paper.pdf", make_pdf(["Alpha words", "Beta words"]))], [], ocr)
        self.assertEqual(seen, [])
        self.assertEqual([r["ocr"] for r in rows], [0])
        self.assertEqual((s.files_ocr, s.pages_ocr), (0, 0))

    def test_blank_pages_keep_the_no_text_row_and_are_not_tried_again(self):
        rows, s = self.build([("scan.pdf", make_pdf(["", ""]))], [], lambda i: "")
        self.assertEqual([(r["position"], r["ocr"]) for r in rows], [(pmod.NO_TEXT, 2)])
        self.assertEqual((s.files_ocr, s.pages_ocr, s.files_no_text), (0, 2, 1))
        seen, ocr = self.reader()
        rows2, s2 = self.build([("scan.pdf", make_pdf(["", ""]))], rows, ocr)
        self.assertEqual((self.fetched, seen, s2.files_kept), ([], [], 1))
        self.assertEqual(rows2, rows)

    def test_a_failed_call_keeps_the_no_text_row_and_is_tried_again(self):
        def boom(image):
            raise llm.PlatformError("HTTP 400 images not supported")
        with self.assertLogs("promoter.passages", "WARNING") as logs:
            rows, s = self.build([("scan.pdf", make_pdf(["", ""])),
                                  ("notes.txt", b"plain words")], [], boom)
        self.assertIn("images not supported", "\n".join(logs.output))
        self.assertEqual([(r["member"], r["position"], r["ocr"]) for r in rows],
                         [("notes.txt", 0, 0), ("scan.pdf", pmod.NO_TEXT, 0)])
        self.assertEqual((s.files_ocr, s.pages_ocr, s.files_no_text, s.passages), (0, 1, 1, 1))
        seen, ocr = self.reader()
        rows2, s2 = self.build([("scan.pdf", make_pdf(["", ""]))], rows, ocr)
        self.assertEqual((self.fetched, len(seen), s2.files_ocr), (["scan.pdf"], 2, 1))

    def test_rows_are_kept_until_the_page_count_changes(self):
        seen, ocr = self.reader()
        scan = [("scan.pdf", make_pdf(["", "", ""]))]
        rows, s = self.build(scan, [], ocr)
        self.assertEqual(len(seen), 2)
        rows2, s2 = self.build(scan, rows, ocr)                 # same checksum, same pages
        self.assertEqual((self.fetched, len(seen), s2.files_kept, s2.files_ocr), ([], 2, 1, 0))
        self.assertEqual(rows2, rows)
        rows3, s3 = self.build(scan, rows, ocr, ocr_pages=3)    # more pages: read again
        self.assertEqual((self.fetched, len(seen), s3.files_ocr, s3.pages_ocr),
                         (["scan.pdf"], 5, 1, 3))
        self.assertEqual([r["ocr"] for r in rows3], [3])
        rows4, s4 = self.build(scan, rows3, None)               # switched off: rows stay
        self.assertEqual((self.fetched, s4.files_kept), ([], 1))
        self.assertEqual(rows4, rows3)
        rows5, s5 = self.build(scan, [], None)                  # a marker written without OCR
        rows6, s6 = self.build(scan, rows5, ocr)                # is read once OCR is on
        self.assertEqual((self.fetched, s6.files_ocr, s6.files_no_text), (["scan.pdf"], 1, 0))

    def test_rows_from_before_the_column_count_as_not_transcribed(self):
        seen, ocr = self.reader()
        files = [("notes.txt", b"plain words")]
        rows, _ = self.build(files, [], None)
        old = [{k: v for k, v in r.items() if k != "ocr"} for r in rows]
        rows2, s2 = self.build(files, old, ocr)
        self.assertEqual((self.fetched, s2.files_kept), ([], 1))
        self.assertEqual([r["ocr"] for r in rows2], [0])

    def test_the_parquet_file_carries_the_column(self):
        seen, ocr = self.reader()
        rows, _ = self.build([("scan.pdf", make_pdf(["", ""])), ("notes.txt", b"plain words")],
                             [], ocr)
        table = pq.read_table(io.BytesIO(pmod.parquet_bytes(rows))).to_pylist()
        self.assertEqual([(r["member"], r["ocr"]) for r in table],
                         [("notes.txt", 0), ("scan.pdf", 2)])


class TestOcrSettings(PassagesBase):
    def test_ocr_is_a_setting_off_by_default(self):
        cfg = PromoterConfig.from_env(dict(self.ENV, **LLM_ENV))
        self.assertEqual((cfg.llm_ocr_model, cfg.ocr_pages), ("", 2))
        cfg = PromoterConfig.from_env(dict(self.ENV, **LLM_ENV, PROMOTER_LLM_OCR_MODEL=" arc:lite ",
                                           PROMOTER_OCR_PAGES="3"))
        self.assertEqual((cfg.llm_ocr_model, cfg.ocr_pages), ("arc:lite", 3))
        for bad in ("0", "-1", "two"):
            with self.assertRaises(ConfigError) as cm:
                PromoterConfig.from_env(dict(self.ENV, **LLM_ENV, PROMOTER_OCR_PAGES=bad))
            self.assertIn("OCR_PAGES", str(cm.exception))

    def test_run_reads_a_scan_through_the_vision_model_when_set(self):
        seen = []

        def fake_transcribe(platform, image, mime="image/jpeg"):
            seen.append((platform.ocr_model, image[:2], mime))
            return "Scanned title words"
        p = mock.patch.object(llm.Platform, "transcribe", fake_transcribe)
        p.start(); self.addCleanup(p.stop)
        on = dict(LLM_ENV, PROMOTER_LLM_OCR_MODEL="arc:lite")
        self.stage(files=self.files)
        code, out = self.cli(["run"], env=on)
        self.assertEqual(code, 0, out)
        s = self.written(out)[0]
        self.assertEqual((s["files_read"], s["files_no_text"], s["files_ocr"], s["pages_ocr"],
                          s["passages"]), (4, 0, 1, 1, 4))
        self.assertEqual(seen, [("arc:lite", b"\xff\xd8", "image/jpeg")])
        by = {r["member"]: r for r in self.rows()}
        self.assertEqual((by["scan.pdf"]["page"], by["scan.pdf"]["position"],
                          by["scan.pdf"]["text"], by["scan.pdf"]["ocr"]),
                         (1, 0, "Scanned title words", 2))
        self.assertEqual(by["notes.txt"]["ocr"], 0)
        # The sweep keeps the rows and its totals carry the two counts.
        code, out = self.cli(["passages"], env=on)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen), 1)
        fin = self.written(out, "passages_finished")[0]
        self.assertEqual((fin["files_ocr"], fin["pages_ocr"], fin["files_no_text"]), (0, 0, 0))
        # Off (the default): the scan is a marker row and nothing is sent.
        code, out = self.cli(["passages", "--dataset", DEST], env=LLM_ENV)
        self.assertEqual(self.written(out)[0]["files_kept"], 4)      # rows stay when off
        self.assertEqual(len(seen), 1)
