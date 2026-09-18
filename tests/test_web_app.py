import os
import unittest
from unittest import mock

try:
    import boto3
    from fastapi.testclient import TestClient
    from moto import mock_aws
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    HAVE_WEB = True
except ImportError:      # web/dev extras not installed: CLI-only checkout
    HAVE_WEB = False

SETTINGS = dict(
    s3_endpoint="https://rgw.example",
    s3_access_key="testing", s3_secret_key="testing",
    s3_bucket="crsw", staging_prefix="staging/_test",
)


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestSkeleton(unittest.TestCase):
    def setUp(self):
        # A developer's ~/.aws/config may set endpoint_url (e.g. to the
        # real Ceph gateway); moto only intercepts AWS-shaped URLs, so
        # point botocore at no config at all for the mocked client.
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "",
            "AWS_ENDPOINT_URL_S3": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="testing",
                               aws_secret_access_key="testing")
        self.s3.create_bucket(Bucket="crsw")
        self.settings = Settings(**SETTINGS)
        self.client = TestClient(create_app(self.settings, s3_client=self.s3))


    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["auth_mode"], "placeholder")
        self.assertRegex(body["version"], r"^\d+\.\d+\.\d+$")

    def test_whoami_is_placeholder_user(self):
        self.assertEqual(self.client.get("/whoami").json(),
                         {"username": "k1078591"})

    def test_connectivity_empty_prefix_is_a_pass(self):
        r = self.client.get("/connectivity")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["sample"], [])
        self.assertEqual(body["prefix"], "staging/_test/")
        self.assertEqual(body["checked_by"], "k1078591")

    def test_connectivity_lists_only_the_staging_prefix(self):
        self.s3.put_object(Bucket="crsw", Key="staging/_test/a.txt", Body=b"x")
        self.s3.put_object(Bucket="crsw", Key="staging/other/b.txt", Body=b"x")
        self.s3.put_object(Bucket="crsw", Key="rs2/csac/c.txt", Body=b"x")
        body = self.client.get("/connectivity").json()
        self.assertEqual(body["sample"], ["staging/_test/a.txt"])

    def test_missing_bucket_is_translated_not_500(self):
        settings = Settings(**dict(SETTINGS, s3_bucket="nope"))
        client = TestClient(create_app(settings, s3_client=self.s3))
        r = client.get("/connectivity")
        self.assertEqual(r.status_code, 502)
        self.assertIn("does not exist", r.json()["detail"])

    def test_oidc_mode_fails_at_startup(self):
        settings = Settings(**dict(SETTINGS, auth_mode="oidc"))
        with self.assertRaises(NotImplementedError):
            create_app(settings, s3_client=self.s3)
