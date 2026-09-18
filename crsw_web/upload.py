"""Stream a request body straight into S3 with bounded memory.

The day-one requirement: nothing in flight touches the VM's disk, and
memory holds at most one part, never the file. Chunks are hashed as
they pass so the checksum label and manifest entry come for free.

Small bodies (up to one part) go up as a single put_object with the
labels attached. Larger ones use S3 multipart; because the checksum is
only known at the end, the labels are then set with a server-side
copy_object onto the same key (MetadataDirective=REPLACE) - no bytes
are re-sent by the service."""
import hashlib
from typing import AsyncIterator, Callable, Dict, Optional, Tuple

from starlette.concurrency import run_in_threadpool

PART_SIZE = 8 * 1024 * 1024        # S3 minimum is 5 MiB except the last part


class TooLarge(Exception):
    def __init__(self, limit: int):
        super().__init__("body exceeds the %d-byte limit" % limit)
        self.limit = limit


async def stream_to_s3(client, bucket: str, key: str,
                       chunks: AsyncIterator[bytes],
                       labels_for: Callable[[str], Dict[str, str]],
                       max_bytes: Optional[int] = None,
                       part_size: int = PART_SIZE) -> Tuple[int, str]:
    """Upload `chunks` to bucket/key. Returns (size, sha256 hex).
    `labels_for(checksum)` supplies the object metadata once the
    checksum is known. Raises TooLarge (after aborting any multipart
    upload) if the body exceeds `max_bytes`."""
    hasher = hashlib.sha256()
    size = 0
    buf = bytearray()
    upload_id = None
    parts = []

    async def flush_part() -> None:
        nonlocal upload_id, buf
        if upload_id is None:
            resp = await run_in_threadpool(
                client.create_multipart_upload, Bucket=bucket, Key=key)
            upload_id = resp["UploadId"]
        number = len(parts) + 1
        data = bytes(buf[:part_size])
        del buf[:part_size]
        resp = await run_in_threadpool(
            client.upload_part, Bucket=bucket, Key=key, UploadId=upload_id,
            PartNumber=number, Body=data)
        parts.append({"PartNumber": number, "ETag": resp["ETag"]})

    try:
        async for chunk in chunks:
            if not chunk:
                continue
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise TooLarge(max_bytes)
            hasher.update(chunk)
            buf.extend(chunk)
            while len(buf) >= part_size:
                await flush_part()

        checksum = hasher.hexdigest()
        labels = labels_for(checksum)
        if upload_id is None:
            await run_in_threadpool(
                client.put_object, Bucket=bucket, Key=key, Body=bytes(buf),
                Metadata=labels)
        else:
            if buf:
                # Last part may be smaller than the minimum.
                number = len(parts) + 1
                resp = await run_in_threadpool(
                    client.upload_part, Bucket=bucket, Key=key,
                    UploadId=upload_id, PartNumber=number, Body=bytes(buf))
                parts.append({"PartNumber": number, "ETag": resp["ETag"]})
                buf = bytearray()
            await run_in_threadpool(
                client.complete_multipart_upload, Bucket=bucket, Key=key,
                UploadId=upload_id, MultipartUpload={"Parts": parts})
            upload_id = None
            await run_in_threadpool(
                client.copy_object, Bucket=bucket, Key=key,
                CopySource={"Bucket": bucket, "Key": key},
                Metadata=labels, MetadataDirective="REPLACE")
        return size, checksum
    except BaseException:
        if upload_id is not None:
            try:
                await run_in_threadpool(
                    client.abort_multipart_upload, Bucket=bucket, Key=key,
                    UploadId=upload_id)
            except Exception:
                pass    # the original error is what matters
        raise
