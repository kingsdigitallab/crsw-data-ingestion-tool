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
    from promoter.config import PromoterConfig

LLM_ENV = {"PROMOTER_LLM_BASE_URL": "https://ai.example/api/v1",
           "PROMOTER_LLM_API_KEY": "sk-test", "PROMOTER_PASSAGES": "1"}
KEY = "index/passages/" + DEST + ".parquet"


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
        self.assertEqual(pmod.chunk([(None, "   ")]), [])
        self.assertEqual(pmod.chunk([(None, "a  b\n c")]), [(None, 0, "a b c")])

    def test_excluded(self):
        self.assertTrue(pmod.excluded("rs1/oral/amber/1_interim/x", ("rs1/oral",)))
        self.assertTrue(pmod.excluded("rs1/oral/amber/1_interim/x", ("rs1/oral/amber/1_interim/x",)))
        self.assertFalse(pmod.excluded("rs1/oralhistory/amber/1_interim/x", ("rs1/oral",)))
        self.assertFalse(pmod.excluded("rs1/oral/amber/1_interim/x", ("",)))


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

    def test_run_writes_passages_from_text_files_only(self):
        self.stage(files=self.files)
        code, out = self.cli(["run"], env=LLM_ENV)
        self.assertEqual(code, 0, out)
        actions = [json.loads(o)["action"] for o in out]
        self.assertEqual(actions[-4:], ["index_written", "embeddings_written",
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
                          by["notes.txt"]["cut"], by["notes.txt"]["sensitivity"]),
                         (3, "arc:embedvl", 1024, "green"))
        self.assertEqual(len(by["notes.txt"]["checksum"]), 64)
        self.assertAlmostEqual(by["notes.txt"]["embedding"][1], 0.5, places=3)
        # What went to the platform: the dataset's own text (embeddings.parquet)
        # and the three passages. Never the CSV, never a file name.
        self.assertEqual(len(self.sent), 4)
        self.assertFalse(any("a,b" in t or ".csv" in t for t in self.sent))

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
