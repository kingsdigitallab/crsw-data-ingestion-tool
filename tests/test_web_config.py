import tempfile
import unittest
from pathlib import Path

try:
    from crsw_web.config import ConfigError, Settings, load_dotenv
    HAVE_WEB = True
except ImportError:      # web extras not installed: CLI-only checkout
    HAVE_WEB = False

GOOD = {
    "CRSW_S3_ENDPOINT": "https://rgw.example",
    "CRSW_S3_ACCESS_KEY": "AK",
    "CRSW_S3_SECRET_KEY": "SK",
    "CRSW_S3_BUCKET": "crsw",
    "CRSW_STAGING_PREFIX": "staging/_test/",
}


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestSettings(unittest.TestCase):
    def test_good_env(self):
        s = Settings.from_env(GOOD)
        self.assertEqual(s.staging_prefix, "staging/_test")  # slash stripped
        self.assertEqual(s.auth_mode, "placeholder")
        self.assertEqual(s.dev_user, "k1078591")

    def test_missing_names_every_variable(self):
        env = dict(GOOD)
        del env["CRSW_S3_SECRET_KEY"]
        env["CRSW_S3_BUCKET"] = "  "
        with self.assertRaises(ConfigError) as cm:
            Settings.from_env(env)
        msg = str(cm.exception)
        self.assertIn("CRSW_S3_SECRET_KEY", msg)
        self.assertIn("CRSW_S3_BUCKET", msg)
        self.assertNotIn("CRSW_S3_ENDPOINT", msg)

    def test_bad_auth_mode(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(GOOD, CRSW_AUTH_MODE="magic"))

    def test_prefix_outside_staging_refused(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(GOOD, CRSW_STAGING_PREFIX="rs2/csac"))

    def test_read_role_off_by_default_and_keys_come_together(self):
        s = Settings.from_env(GOOD)
        self.assertFalse(s.read_enabled)
        self.assertEqual((s.index_prefix, s.index_refresh_seconds, s.amber_access),
                         ("index", 60, "off"))
        with self.assertRaises(ConfigError) as cm:
            Settings.from_env(dict(GOOD, CRSW_READ_S3_ACCESS_KEY="RK"))
        self.assertIn("READ_S3_SECRET_KEY", str(cm.exception))
        s = Settings.from_env(dict(GOOD, CRSW_READ_S3_ACCESS_KEY="RK",
                                   CRSW_READ_S3_SECRET_KEY="RS", CRSW_INDEX_PREFIX="/idx/",
                                   CRSW_DEV_GROUPS="a, er_prj_kdl_slavery_rs2"))
        self.assertTrue(s.read_enabled)
        self.assertEqual(s.index_prefix, "idx")
        self.assertEqual(s.dev_groups, ("a", "er_prj_kdl_slavery_rs2"))

    def test_amber_access_modes(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(GOOD, CRSW_AMBER_ACCESS="maybe"))
        self.assertEqual(Settings.from_env(dict(GOOD, CRSW_AMBER_ACCESS="all")).amber_access, "all")
        with self.assertRaises(ConfigError):     # template without the strand
            Settings.from_env(dict(GOOD, CRSW_AMBER_ACCESS="groups",
                                   CRSW_AMBER_GROUP_TEMPLATE="er_prj_kdl_slavery"))
        with self.assertRaises(ConfigError):     # proxy mode needs the groups header
            Settings.from_env(dict(GOOD, CRSW_AMBER_ACCESS="groups", CRSW_AUTH_MODE="proxy",
                                   CRSW_PROXY_USER_HEADER="X-Remote-User"))
        s = Settings.from_env(dict(GOOD, CRSW_AMBER_ACCESS="groups", CRSW_AUTH_MODE="proxy",
                                   CRSW_PROXY_USER_HEADER="X-Remote-User",
                                   CRSW_PROXY_GROUPS_HEADER="X-Remote-Groups"))
        self.assertEqual(s.amber_group_template, "er_prj_kdl_slavery_{strand}")


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestDotenv(unittest.TestCase):
    def test_reads_without_overriding(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# comment\nA=1\nB = 'two'\nbad line\nC=\n",
                         encoding="utf-8")
            env = {"A": "already"}
            n = load_dotenv(p, env)
            self.assertEqual(env, {"A": "already", "B": "two", "C": ""})
            self.assertEqual(n, 2)

    def test_absent_file_is_fine(self):
        self.assertEqual(load_dotenv("/no/such/.env", {}), 0)
