import os
import tempfile
import unittest

from crsw_deposit import filetypes as ft

PE_HEAD = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 48


class TestExtension(unittest.TestCase):
    def test_last_extension_lower_cased(self):
        self.assertEqual(ft.extension("a/b/Setup.EXE"), "exe")
        self.assertEqual(ft.extension("report.pdf.exe"), "exe")

    def test_trailing_dots_and_spaces_ignored(self):
        # Windows runs "x.exe." and "x.exe " as x.exe.
        self.assertEqual(ft.extension("x.exe."), "exe")
        self.assertEqual(ft.extension("x.exe  "), "exe")

    def test_no_extension(self):
        for name in ("README", ".gitignore", "a.b/README", ""):
            self.assertEqual(ft.extension(name), "", name)

    def test_only_the_file_name_counts(self):
        self.assertEqual(ft.extension("tools.exe/data.csv"), "csv")


class TestRefusedName(unittest.TestCase):
    def test_every_listed_extension_is_refused(self):
        for ext, kind in ft.REFUSED_EXTENSIONS.items():
            reason = ft.refused_name("dir/file." + ext)
            self.assertEqual(reason, "%s (.%s)" % (kind, ext))

    def test_examples_by_kind(self):
        self.assertEqual(ft.refused_name("setup.exe"), "a Windows program (.exe)")
        self.assertEqual(ft.refused_name("a/Tool.MSI"), "an installer (.msi)")
        self.assertEqual(ft.refused_name("run_all.sh"), "a system script (.sh)")
        self.assertEqual(ft.refused_name("lib.jar"), "a Java program (.jar)")
        self.assertEqual(ft.refused_name("data.lnk"), "a shortcut (.lnk)")
        self.assertEqual(ft.refused_name("book.xlsm"),
                         "an Office file with macros (.xlsm)")

    def test_double_extension_and_trailing_dot(self):
        self.assertIsNotNone(ft.refused_name("report.pdf.exe"))
        self.assertIsNotNone(ft.refused_name("x.exe."))

    def test_analysis_code_and_data_are_allowed(self):
        for name in ("clean.py", "model.R", "analysis.do", "nb.ipynb",
                     "q.sql", "plot.js", "raster.bin", "fit.out", "disk.img",
                     "book.xlsb", "old.doc", "old.xls", "page.html", "fig.svg",
                     "data.csv", "paper.docx", "README", ".gitignore"):
            self.assertIsNone(ft.refused_name(name), name)

    def test_directory_named_like_a_program_refuses_nothing(self):
        self.assertIsNone(ft.refused_name("tools.exe/data.csv"))

    def test_no_extension_is_listed_twice(self):
        self.assertFalse(set(ft.REFUSED_EXTENSIONS) & ft.ARCHIVE_EXTENSIONS)


class TestArchive(unittest.TestCase):
    def test_archives(self):
        for name in ("a.zip", "b.tar.gz", "c.7z", "D.RAR", "e.tgz", "f.iso"):
            self.assertTrue(ft.is_archive(name), name)

    def test_not_archives(self):
        for name in ("a.csv", "zip", "notes.zip.txt"):
            self.assertFalse(ft.is_archive(name), name)

    def test_archives_are_not_refused(self):
        for ext in ft.ARCHIVE_EXTENSIONS:
            self.assertIsNone(ft.refused_name("x." + ext), ext)


class TestRefusedContent(unittest.TestCase):
    def test_windows_program(self):
        self.assertEqual(ft.refused_content(PE_HEAD), "a Windows program")

    def test_text_starting_mz_is_allowed(self):
        # Mozambique's country code opens many a CSV.
        self.assertIsNone(ft.refused_content(b"MZ,2019,31.2\nZA,2019,58.1\n"))
        self.assertIsNone(ft.refused_content(b"MZ"))

    def test_linux_program(self):
        self.assertEqual(ft.refused_content(b"\x7fELF\x02\x01\x01" + b"\x00" * 9),
                         "a Linux program")

    def test_every_magic(self):
        for magic, kind in ft.MAGICS:
            self.assertEqual(ft.refused_content(magic + b"\x00" * 28), kind)

    def test_shell_shebangs_refused(self):
        for line in (b"#!/bin/sh\n", b"#!/bin/bash -e\necho hi\n",
                     b"#! /usr/bin/env bash\n", b"#!/usr/bin/env -S bash -e\n",
                     b"#!/usr/bin/env FOO=1 zsh\n", b"#!/bin/sh\r\necho\r\n",
                     b"#!/usr/local/bin/fish", b"#!/usr/bin/pwsh\n"):
            self.assertEqual(ft.refused_content(line), "a system script", line)

    def test_other_shebangs_allowed(self):
        for line in (b"#!/usr/bin/env python\n", b"#!/usr/bin/python3\n",
                     b"#!/usr/bin/env Rscript\n", b"#!/usr/bin/perl -w\n",
                     b"#!/usr/bin/env\n", b"#!\n", b"#! \n"):
            self.assertIsNone(ft.refused_content(line), line)

    def test_shell_named_later_in_the_file_is_not_a_shebang(self):
        self.assertIsNone(ft.refused_content(b"# run with /bin/sh\n#!/bin/sh\n"))
        self.assertIsNone(ft.refused_content(b"#!/usr/bin/env python\nbash\n"))

    def test_ordinary_openings_allowed(self):
        for head in (b"", b"M", b"#", b"%PDF-1.7\n", b"PK\x03\x04",
                     b"id,name\n1,a\n", b"\xef\xbb\xbfid,name\n",
                     b"\x89PNG\r\n\x1a\n", b"\x00" * 64):
            self.assertIsNone(ft.refused_content(head), head)

    def test_sniff_length_covers_the_tests(self):
        self.assertGreaterEqual(ft.SNIFF_BYTES, ft.WINDOWS_HEADER_BYTES)
        self.assertGreaterEqual(ft.SNIFF_BYTES, 256)


class TestSniffFile(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def test_renamed_program(self):
        self.assertEqual(ft.sniff_file(self._write(PE_HEAD + b"x" * 2000)),
                         "a Windows program")

    def test_plain_and_empty_files(self):
        self.assertIsNone(ft.sniff_file(self._write(b"a,b\n1,2\n")))
        self.assertIsNone(ft.sniff_file(self._write(b"")))

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            ft.sniff_file(os.path.join(tempfile.gettempdir(), "no-such-crsw-file"))


if __name__ == "__main__":
    unittest.main()
