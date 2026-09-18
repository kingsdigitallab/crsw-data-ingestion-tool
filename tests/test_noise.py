import unittest

from crsw_deposit import noise


class TestNoiseFiles(unittest.TestCase):
    def test_known_names(self):
        for name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            self.assertTrue(noise.is_noise_file(name), name)

    def test_appledouble_prefix(self):
        self.assertTrue(noise.is_noise_file("._report.docx"))

    def test_ordinary_files_and_dotfiles_are_not_noise(self):
        # Hidden files are deliberately deposited (r7 §2); only the
        # named OS artefacts are excluded.
        for name in ("report.docx", ".gitignore", ".env.example", "_x"):
            self.assertFalse(noise.is_noise_file(name), name)


class TestNoiseDirs(unittest.TestCase):
    def test_known_dirs(self):
        for name in (".git", "__pycache__", "__MACOSX", "$RECYCLE.BIN"):
            self.assertTrue(noise.is_noise_dir(name), name)

    def test_ordinary_dirs(self):
        self.assertFalse(noise.is_noise_dir("data"))
        self.assertFalse(noise.is_noise_dir(".github"))


class TestNoiseMember(unittest.TestCase):
    def test_noise_file_at_any_depth(self):
        self.assertTrue(noise.is_noise_member("2024/tiles/.DS_Store"))

    def test_file_inside_noise_dir(self):
        self.assertTrue(noise.is_noise_member(".git/config"))
        self.assertTrue(noise.is_noise_member("a/__MACOSX/b.tif"))

    def test_noise_dir_name_as_a_file_is_a_file(self):
        # Only directory segments are checked against the dir set.
        self.assertFalse(noise.is_noise_member("data/__MACOSX"))

    def test_clean_member(self):
        self.assertFalse(noise.is_noise_member("2024/tiles/a.tif"))


if __name__ == "__main__":
    unittest.main()
