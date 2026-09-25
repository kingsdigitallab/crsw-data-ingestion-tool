"""The service's one S3 client factory. Ceph specifics live here and
nowhere else: path-style addressing, SigV4, the configured endpoint.

Two clients can exist: the staging-only one every upload handler uses,
and, when the read role is on, a read-only one over the read key. The
read-only one is wrapped so that no code path can write with it, which
matters while a broad personal key stands in for the scoped key."""
from typing import List, Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from .config import Settings


def make_client(settings: Settings, access_key: Optional[str] = None,
                secret_key: Optional[str] = None):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=access_key or settings.s3_access_key,
        aws_secret_access_key=secret_key or settings.s3_secret_key,
        region_name="us-east-1",   # RGW ignores it; boto3 insists on one
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class ReadOnly:
    """The read role's client: get, head and list, nothing else. Any other
    name is an AttributeError, so a bug cannot write or delete with the
    read key however broad that key happens to be."""

    READS = ("get_object", "head_object")

    def __init__(self, client):
        self._client = client

    def get_object(self, **kw):
        return self._client.get_object(**kw)

    def head_object(self, **kw):
        return self._client.head_object(**kw)

    def get_paginator(self, name: str):
        if name != "list_objects_v2":
            raise AttributeError("read-only client: no paginator %r" % name)
        return self._client.get_paginator(name)

    def __getattr__(self, name):
        raise AttributeError("read-only client has no %r" % name)


def make_read_client(settings: Settings) -> ReadOnly:
    return ReadOnly(make_client(settings, settings.read_s3_access_key,
                                settings.read_s3_secret_key))


def list_prefix(client, bucket: str, prefix: str,
                limit: Optional[int] = None) -> List[str]:
    """Object keys under `prefix` (a trailing '/' is added if missing),
    lexicographic, up to `limit`. Raises botocore errors unchanged;
    callers translate them."""
    if not prefix.endswith("/"):
        prefix += "/"
    keys: List[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
            if limit is not None and len(keys) >= limit:
                return keys
    return keys


def translate_error(exc: Exception, settings: Settings) -> str:
    """A human-readable reason, in the CLI's spirit (spec §8): name the
    likely cause, never just echo an S3 error code."""
    if isinstance(exc, EndpointConnectionError):
        return ("cannot reach %s. From a laptop this usually means the KCL "
                "VPN is not connected." % settings.s3_endpoint)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "InvalidAccessKeyId",
                    "SignatureDoesNotMatch"):
            return ("the service key was rejected for bucket %r (%s). Check "
                    "CRSW_S3_ACCESS_KEY / CRSW_S3_SECRET_KEY and that the "
                    "key may list %s/." % (settings.s3_bucket, code,
                                           settings.staging_prefix))
        if code == "NoSuchBucket":
            return "bucket %r does not exist on %s" % (settings.s3_bucket,
                                                         settings.s3_endpoint)
        return "storage error %s: %s" % (code or "unknown", exc)
    return "unexpected error: %s" % exc
