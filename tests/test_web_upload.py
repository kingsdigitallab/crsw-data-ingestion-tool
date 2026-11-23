import asyncio
import hashlib
import os
import unittest
from unittest import mock

try:
    import boto3
    from moto import mock_aws
    from crsw_web.upload import Refused, TooLarge, stream_to_s3
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

PART = 5 * 1024 * 1024   # the S3 minimum part size; moto enforces it too


async def gen(data: bytes, chunk: int = 64 * 1024):
    for i in range(0, len(data), chunk):
        yield data[i:i + chunk]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@unittest.skipUnless(HAVE_WEB, "web extras not installed")
class TestStreamToS3(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name="us-east-1",
                               aws_access_key_id="t", aws_secret_access_key="t")
        self.s3.create_bucket(Bucket="bkt")


    def labels(self, checksum):
        return {"checksum-sha256": checksum, "depositor": "k1"}

    def check_object(self, key, data):
        head = self.s3.head_object(Bucket="bkt", Key=key)
        self.assertEqual(head["ContentLength"], len(data))
        self.assertEqual(head["Metadata"]["checksum-sha256"],
                         hashlib.sha256(data).hexdigest())
        self.assertEqual(head["Metadata"]["depositor"], "k1")
        self.assertEqual(self.s3.get_object(Bucket="bkt", Key=key)["Body"].read(),
                         data)

    def test_small_body_single_put_with_labels(self):
        data = b"hello world\n" * 100
        size, checksum = run(stream_to_s3(
            self.s3, "bkt", "k/small.txt", gen(data), self.labels, part_size=PART))
        self.assertEqual((size, checksum), (len(data), hashlib.sha256(data).hexdigest()))
        self.check_object("k/small.txt", data)

    def test_empty_body(self):
        size, checksum = run(stream_to_s3(
            self.s3, "bkt", "k/empty", gen(b""), self.labels, part_size=PART))
        self.assertEqual(size, 0)
        self.check_object("k/empty", b"")

    def test_large_body_multipart_with_labels_after(self):
        # 2.5 parts: two full parts plus a short last one.
        data = os.urandom(PART * 2 + PART // 2)
        size, checksum = run(stream_to_s3(
            self.s3, "bkt", "k/big.bin", gen(data), self.labels, part_size=PART))
        self.assertEqual(size, len(data))
        self.check_object("k/big.bin", data)
        # Nothing left dangling.
        self.assertNotIn("Uploads", self.s3.list_multipart_uploads(Bucket="bkt"))

    def test_exact_part_multiple(self):
        data = os.urandom(PART * 2)
        run(stream_to_s3(self.s3, "bkt", "k/exact", gen(data), self.labels,
                         part_size=PART))
        self.check_object("k/exact", data)

    def test_too_large_is_refused_and_aborted(self):
        data = os.urandom(PART * 2)
        with self.assertRaises(TooLarge):
            run(stream_to_s3(self.s3, "bkt", "k/toobig", gen(data), self.labels,
                             max_bytes=PART + 10, part_size=PART))
        self.assertNotIn("Uploads", self.s3.list_multipart_uploads(Bucket="bkt"))
        with self.assertRaises(self.s3.exceptions.ClientError):
            self.s3.head_object(Bucket="bkt", Key="k/toobig")

    # -- head_check (r13): the opening bytes are shown to a checker
    #    before anything is written.

    def absent(self, key):
        with self.assertRaises(self.s3.exceptions.ClientError):
            self.s3.head_object(Bucket="bkt", Key=key)
        self.assertNotIn("Uploads", self.s3.list_multipart_uploads(Bucket="bkt"))

    def test_head_assembled_across_tiny_chunks_and_refused(self):
        data = b"MZ" + bytes(range(200)) * 5
        seen = []

        def checker(head):
            seen.append(head)
            return "a program"
        with self.assertRaises(Refused) as cm:
            run(stream_to_s3(self.s3, "bkt", "k/r1", gen(data, chunk=1),
                             self.labels, part_size=PART,
                             head_check=checker, head_bytes=512))
        self.assertEqual(cm.exception.reason, "a program")
        self.assertEqual(seen, [data[:512]])
        self.absent("k/r1")

    def test_large_refused_body_leaves_nothing(self):
        data = os.urandom(PART * 2)
        with self.assertRaises(Refused):
            run(stream_to_s3(self.s3, "bkt", "k/r2", gen(data), self.labels,
                             part_size=PART, head_check=lambda h: "no",
                             head_bytes=512))
        self.absent("k/r2")

    def test_short_body_is_still_checked(self):
        seen = []

        def checker(head):
            seen.append(head)
            return "no"
        with self.assertRaises(Refused):
            run(stream_to_s3(self.s3, "bkt", "k/r3", gen(b"#!/bin/sh\n"),
                             self.labels, part_size=PART,
                             head_check=checker, head_bytes=512))
        self.assertEqual(seen, [b"#!/bin/sh\n"])
        self.absent("k/r3")

    def test_refused_replacement_keeps_the_earlier_copy(self):
        run(stream_to_s3(self.s3, "bkt", "k/keep", gen(b"good"), self.labels,
                         part_size=PART))
        with self.assertRaises(Refused):
            run(stream_to_s3(self.s3, "bkt", "k/keep", gen(b"bad"), self.labels,
                             part_size=PART, head_check=lambda h: "no",
                             head_bytes=512))
        self.check_object("k/keep", b"good")

    def test_passing_check_is_asked_once_and_stores_as_before(self):
        data = os.urandom(PART + 100)
        calls = []

        def checker(head):
            calls.append(len(head))
            return None
        run(stream_to_s3(self.s3, "bkt", "k/ok", gen(data), self.labels,
                         part_size=PART, head_check=checker, head_bytes=512))
        self.assertEqual(calls, [512])
        self.check_object("k/ok", data)

    def test_empty_body_is_checked_with_nothing(self):
        calls = []
        run(stream_to_s3(self.s3, "bkt", "k/e", gen(b""), self.labels,
                         part_size=PART, head_check=lambda h: calls.append(h),
                         head_bytes=512))
        self.assertEqual(calls, [b""])
        self.check_object("k/e", b"")

    def test_memory_bound_one_part(self):
        # The buffer never holds more than one part plus one chunk.
        seen = []
        orig = self.s3.upload_part

        def spy(**kw):
            seen.append(len(kw["Body"]))
            return orig(**kw)
        with mock.patch.object(self.s3, "upload_part", side_effect=spy):
            run(stream_to_s3(self.s3, "bkt", "k/x", gen(os.urandom(PART * 3 + 5)),
                             self.labels, part_size=PART))
        self.assertEqual(seen, [PART, PART, PART, 5])
