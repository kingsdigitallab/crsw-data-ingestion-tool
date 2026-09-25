import os
import unittest
from unittest import mock

try:
    import boto3
    from fastapi.testclient import TestClient
    from moto import mock_aws
    from crsw_web.app import create_app
    from crsw_web.access import may_download
    from crsw_web.auth import (NotAuthenticated, NotAuthorised, User, peer_is_trusted,
                               user_from_headers)
    from crsw_web.config import ConfigError, Settings
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

BASE = dict(s3_endpoint="https://rgw.example", s3_access_key="t", s3_secret_key="t",
            s3_bucket="crsw", staging_prefix="staging/_test")
PROXY = dict(BASE, auth_mode="proxy", proxy_user_header="X-Remote-User")


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestUserFromHeaders(unittest.TestCase):
    def test_plain_username(self):
        s = Settings(**PROXY)
        self.assertEqual(user_from_headers({"X-Remote-User": "K1078591"}, s).username,
                         "k1078591")

    def test_missing_header(self):
        with self.assertRaises(NotAuthenticated):
            user_from_headers({}, Settings(**PROXY))
        with self.assertRaises(NotAuthenticated):
            user_from_headers({"X-Remote-User": "  "}, Settings(**PROXY))

    def test_pattern_extracts_knumber(self):
        s = Settings(**dict(PROXY, proxy_username_pattern=r"^([a-z]\d+)@kcl\.ac\.uk$"))
        self.assertEqual(user_from_headers({"X-Remote-User": "k1078591@kcl.ac.uk"}, s).username,
                         "k1078591")
        with self.assertRaises(NotAuthorised):
            user_from_headers({"X-Remote-User": "someone@example.org"}, s)

    def test_groups_header_enforced_only_when_configured(self):
        s = Settings(**PROXY)   # no groups header: proxy's Allowed groups gates access
        user_from_headers({"X-Remote-User": "k1"}, s)
        s = Settings(**dict(PROXY, proxy_groups_header="X-Remote-Groups"))
        with self.assertRaises(NotAuthorised):
            user_from_headers({"X-Remote-User": "k1", "X-Remote-Groups": "staff,other"}, s)
        ok = user_from_headers({"X-Remote-User": "k1",
                                "X-Remote-Groups": "staff; er_prj_kdl_slavery"}, s)
        self.assertEqual(ok.username, "k1")


    def test_groups_travel_with_the_user(self):
        s = Settings(**dict(PROXY, proxy_groups_header="X-Remote-Groups"))
        u = user_from_headers({"X-Remote-User": "k1",
                               "X-Remote-Groups": "er_prj_kdl_slavery, er_prj_kdl_slavery_rs2"}, s)
        self.assertEqual(u.groups, ("er_prj_kdl_slavery", "er_prj_kdl_slavery_rs2"))
        self.assertEqual(user_from_headers({"X-Remote-User": "k1"}, Settings(**PROXY)).groups, ())


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestMayDownload(unittest.TestCase):
    """The amber rule, finding-and-reuse.md §1."""

    def test_green_always_amber_by_mode_red_never(self):
        s = Settings(**BASE)
        u = User("k1")
        self.assertIsNone(may_download(u, "green", "rs1", s))
        self.assertIn("steward", may_download(u, "amber", "rs1", s))
        self.assertIn("not served", may_download(u, "red", "rs1", s))
        s = Settings(**dict(BASE, amber_access="all"))
        self.assertIsNone(may_download(u, "amber", "rs1", s))

    def test_groups_mode_uses_the_strand_group(self):
        s = Settings(**dict(BASE, amber_access="groups"))
        self.assertIn("er_prj_kdl_slavery_rs2",
                      may_download(User("k1", ("er_prj_kdl_slavery",)), "amber", "rs2", s))
        self.assertIsNone(may_download(User("k1", ("er_prj_kdl_slavery_rs2",)), "amber", "rs2", s))
        self.assertIsNotNone(may_download(User("k1", ("er_prj_kdl_slavery_rs2",)), "amber", "rs3", s))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestPeer(unittest.TestCase):
    def test_no_cidrs_means_no_check(self):
        self.assertTrue(peer_is_trusted("203.0.113.9", []))

    def test_cidrs(self):
        nets = Settings(**dict(PROXY, trusted_proxy_cidrs=("10.0.0.0/8", "192.0.2.5/32"))
                        ).trusted_proxy_networks()
        self.assertTrue(peer_is_trusted("10.211.118.41", nets))
        self.assertTrue(peer_is_trusted("192.0.2.5", nets))
        self.assertFalse(peer_is_trusted("203.0.113.9", nets))
        self.assertFalse(peer_is_trusted("testclient", nets))
        self.assertFalse(peer_is_trusted(None, nets))


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestProxyMode(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.mock = mock_aws(); self.mock.start(); self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="crsw")

    def app(self, **overrides):
        return create_app(Settings(**dict(PROXY, **overrides)), s3_client=self.s3,
                          vocab_dict={"facets": {}, "domains": []})

    def test_header_gives_identity(self):
        c = TestClient(self.app())
        r = c.get("/whoami", headers={"X-Remote-User": "k1078591"})
        self.assertEqual(r.json(), {"username": "k1078591", "auth_mode": "proxy"})

    def test_missing_header_is_401_on_api_and_page(self):
        c = TestClient(self.app())
        self.assertEqual(c.get("/whoami").status_code, 401)
        self.assertEqual(c.get("/").status_code, 401)
        self.assertEqual(c.post("/deposits", json={}).status_code, 401)
        self.assertEqual(c.get("/health").status_code, 200)   # unauthenticated by design

    def test_untrusted_peer_is_403_even_with_header(self):
        app = self.app(trusted_proxy_cidrs=("10.0.0.0/8",))
        good = TestClient(app, client=("10.211.118.41", 1234))
        self.assertEqual(good.get("/whoami", headers={"X-Remote-User": "k1"}).status_code, 200)
        bad = TestClient(app, client=("203.0.113.9", 1234))
        r = bad.get("/whoami", headers={"X-Remote-User": "k1"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("KCL proxy", r.json()["detail"])

    def test_sidecar_peer_header_wins_over_forwarded_client(self):
        # Behind the KCL proxy, uvicorn's client address is the browser
        # (leftmost X-Forwarded-For); the sidecar's own peer decides.
        app = self.app(trusted_proxy_cidrs=("10.202.65.116/32",))
        c = TestClient(app, client=("10.202.65.81", 1234))       # the browser
        self.assertEqual(c.get("/whoami", headers={
            "X-Remote-User": "k1",
            "X-Sidecar-Peer": "10.202.65.116"}).status_code, 200)
        self.assertEqual(c.get("/whoami", headers={
            "X-Remote-User": "k1",
            "X-Sidecar-Peer": "10.202.65.81"}).status_code, 403)
        self.assertEqual(c.get("/whoami", headers={
            "X-Remote-User": "k1"}).status_code, 403)

    def test_group_refusal_is_403_with_reason(self):
        c = TestClient(self.app(proxy_groups_header="X-Remote-Groups"))
        r = c.get("/whoami", headers={"X-Remote-User": "k1", "X-Remote-Groups": "staff"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("er_prj_kdl_slavery", r.json()["detail"])

    def test_debug_headers_only_when_enabled(self):
        c = TestClient(self.app())
        self.assertEqual(c.get("/auth/headers").status_code, 404)
        c = TestClient(self.app(debug_headers=True))
        r = c.get("/auth/headers", headers={"X-Remote-User": "k1", "Cookie": "s=1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["headers"]["x-remote-user"], "k1")
        self.assertNotIn("cookie", r.json()["headers"])

    def test_placeholder_mode_unchanged(self):
        c = TestClient(create_app(Settings(**BASE), s3_client=self.s3,
                                  vocab_dict={"facets": {}, "domains": []}))
        self.assertEqual(c.get("/whoami").json()["username"], "k1078591")


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestProxyConfig(unittest.TestCase):
    ENV = {"CRSW_S3_ENDPOINT": "e", "CRSW_S3_ACCESS_KEY": "a", "CRSW_S3_SECRET_KEY": "s",
           "CRSW_S3_BUCKET": "b", "CRSW_STAGING_PREFIX": "staging/x"}

    def test_proxy_mode_needs_header_name(self):
        with self.assertRaises(ConfigError) as cm:
            Settings.from_env(dict(self.ENV, CRSW_AUTH_MODE="proxy"))
        self.assertIn("CRSW_PROXY_USER_HEADER", str(cm.exception))
        s = Settings.from_env(dict(self.ENV, CRSW_AUTH_MODE="proxy",
                                   CRSW_PROXY_USER_HEADER="X-Remote-User",
                                   CRSW_TRUSTED_PROXY_CIDRS="10.0.0.0/8, 192.0.2.0/24",
                                   CRSW_USER_QUOTA_BYTES="100", CRSW_DEBUG_HEADERS="1"))
        self.assertEqual(s.trusted_proxy_cidrs, ("10.0.0.0/8", "192.0.2.0/24"))
        self.assertEqual(s.user_quota_bytes, 100)
        self.assertTrue(s.debug_headers)

    def test_bad_cidr_and_pattern(self):
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(self.ENV, CRSW_TRUSTED_PROXY_CIDRS="not-a-net"))
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(self.ENV, CRSW_PROXY_USERNAME_PATTERN="no-group"))
        with self.assertRaises(ConfigError):
            Settings.from_env(dict(self.ENV, CRSW_PROXY_USERNAME_PATTERN="(unclosed"))
