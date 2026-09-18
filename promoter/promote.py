"""Move a checked deposit from staging to its dataset prefix.

Members first, record last; verify at the destination; only then add
delete markers in staging (control object last, so a half-finished
promotion is still visible). Copies are server-side and idempotent, so
a failed run can simply be re-run."""
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Set

from crsw_deposit import deposit_logic, keys, labels as labels_mod, record
from crsw_web.deposits import Deposit, DepositStore

from .checks import Report
from .log import Log

# Server-side copy_object is limited to 5 GB; larger objects use
# multipart copy in ranges of this size.
SINGLE_COPY_MAX = 5 * 1024 ** 3
COPY_PART = 512 * 1024 ** 2


class PromotionError(RuntimeError):
    pass


@dataclass
class Outcome:
    promoted: bool
    dataset_uuid: str = ""
    copied: List[str] = field(default_factory=list)
    record_key: str = ""
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    error: str = ""


def _copy(client, bucket, src: str, dst: str, size: int,
          metadata: Dict[str, str], single_max: int = SINGLE_COPY_MAX,
          part: int = COPY_PART) -> None:
    source = {"Bucket": bucket, "Key": src}
    if size <= single_max:
        client.copy_object(Bucket=bucket, Key=dst, CopySource=source,
                           Metadata=metadata, MetadataDirective="REPLACE")
        return
    upload = client.create_multipart_upload(Bucket=bucket, Key=dst, Metadata=metadata)
    parts = []
    try:
        number = 1
        for start in range(0, size, part):
            end = min(start + part, size) - 1
            resp = client.upload_part_copy(
                Bucket=bucket, Key=dst, UploadId=upload["UploadId"],
                PartNumber=number, CopySource=source,
                CopySourceRange="bytes=%d-%d" % (start, end))
            parts.append({"PartNumber": number, "ETag": resp["CopyPartResult"]["ETag"]})
            number += 1
        client.complete_multipart_upload(Bucket=bucket, Key=dst,
                                         UploadId=upload["UploadId"],
                                         MultipartUpload={"Parts": parts})
    except BaseException:
        client.abort_multipart_upload(Bucket=bucket, Key=dst, UploadId=upload["UploadId"])
        raise


def promote(dep: Deposit, rep: Report, store: DepositStore, log: Log,
            vocab_terms: Set[str], domain_codes: List[str],
            keep_staging: bool = False, single_copy_max: int = SINGLE_COPY_MAX,
            copy_part: int = COPY_PART) -> Outcome:
    if not rep.ok:
        raise PromotionError("refusing to promote a deposit with problems")
    client, bucket = store.client, store.bucket
    existing = rep.existing_record
    dataset_uuid = existing["dataset_uuid"] if existing else dep.dataset_uuid
    out = Outcome(promoted=False, dataset_uuid=dataset_uuid)
    staged_prefix = store.staged_key(dep.user, dep.id, dep.prefix)

    try:
        # Members first.
        for entry in dep.entries:
            src = staged_prefix + "/" + entry["path"]
            dst = keys.build_key(dep.meta["strand"], dep.meta["project"],
                                 dep.meta["sensitivity"], dep.meta["state"],
                                 dep.meta["dataset"], entry["path"])
            labels = labels_mod.object_labels(dataset_uuid, entry["checksum_sha256"],
                                              dep.meta["sensitivity"], dep.user)
            _copy(client, bucket, src, dst, entry["bytes"], labels,
                  single_max=single_copy_max, part=copy_part)
            size = store.stored_size(dst)
            if size != entry["bytes"]:
                raise PromotionError("copied %s but destination size is %s, expected %d"
                                     % (dst, size, entry["bytes"]))
            out.copied.append(dst)
            log.write("copied", dep, source=src, destination=dst, bytes=entry["bytes"])

        # Record last, reconciled with whatever is already there.
        rec, union, added, updated = deposit_logic.assemble_record(
            dep.meta, existing, dep.entries, dep.user, record.utc_now_iso(),
            dataset_uuid)
        errors, _ = record.validate_record(rec, vocab_terms, domain_codes)
        if errors:
            raise PromotionError("assembled record invalid: %s" % "; ".join(errors))
        data = deposit_logic.record_bytes(rec)
        record_key = keys.record_key(dep.meta["strand"], dep.meta["project"],
                                     dep.meta["sensitivity"], dep.meta["state"],
                                     dep.meta["dataset"])
        client.put_object(
            Bucket=bucket, Key=record_key, Body=data, ContentType="application/json",
            Metadata=labels_mod.object_labels(dataset_uuid, hashlib.sha256(data).hexdigest(),
                                              dep.meta["sensitivity"], dep.user))
        back = client.get_object(Bucket=bucket, Key=record_key)["Body"].read()
        if back != data:
            raise PromotionError("record round-trip mismatch at %s" % record_key)
        out.record_key = record_key
        out.added, out.updated = added, updated
        log.write("record_written", dep, destination=record_key, files=len(union),
                  added=len(added), updated=len(updated),
                  merged_with_existing=bool(existing))

        # Completion check at the destination over the whole manifest.
        problems = deposit_logic.completion_problems(dep.prefix, union, store.stored_size)
        if problems:
            raise PromotionError("destination incomplete after promotion: %s"
                                 % "; ".join(problems))
    except Exception as e:
        out.error = str(e)
        log.write("promotion_failed", dep, error=out.error, copied=out.copied)
        return out

    out.promoted = True
    if keep_staging:
        log.write("promoted_kept_staging", dep, dataset_uuid=dataset_uuid)
        return out

    # Delete markers in staging: members, record, control object last.
    for entry in dep.entries:
        store.delete(staged_prefix + "/" + entry["path"])
    store.delete(dep.record_key)
    store.delete(store.control_key(dep.user, dep.id))
    log.write("promoted", dep, dataset_uuid=dataset_uuid,
              files=len(dep.entries), record=record_key)
    return out
