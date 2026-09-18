"""Every check a deposit must pass before it leaves staging. Each failure
is a named problem; the deposit is left where it is and reported.

Nothing here moves or deletes anything."""
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from botocore.exceptions import ClientError

from crsw_deposit import deposit_logic, keys, labels as labels_mod, record
from crsw_web.deposits import STATUS_COMPLETE, Deposit, DepositStore

from .config import PromoterConfig

CHUNK = 8 * 1024 * 1024


@dataclass
class Report:
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    existing_record: Optional[Dict] = None
    staged_record: Optional[Dict] = None
    total_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _read_json_object(client, bucket, key):
    """(text, None) or (None, 'absent') or (None, error)."""
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None, "absent"
        return None, code or str(e)
    return obj["Body"].read().decode("utf-8"), None


def _sha256_of_object(client, bucket, key) -> str:
    h = hashlib.sha256()
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in body.iter_chunks(CHUNK):
        h.update(chunk)
    return h.hexdigest()


def check(dep: Deposit, store: DepositStore, cfg: PromoterConfig,
          vocab_terms: Set[str], domain_codes: List[str],
          verify_checksums: bool = True) -> Report:
    rep = Report()
    client, bucket = store.client, store.bucket
    p = rep.problems

    # 1. control object state
    if dep.status != STATUS_COMPLETE:
        p.append("deposit status is %r, not complete" % dep.status)
    if not dep.record_key:
        p.append("deposit has no record_key")
    if not dep.entries:
        p.append("deposit has no files")
    if p:
        return rep

    staged_prefix = store.staged_key(dep.user, dep.id, dep.prefix)

    # 2. the staged record
    expected_record_key = staged_prefix + "/" + keys.record_filename(dep.meta["dataset"])
    if dep.record_key != expected_record_key:
        p.append("record_key %r is not where the layout says (%r)"
                 % (dep.record_key, expected_record_key))
    text, err = _read_json_object(client, bucket, dep.record_key)
    staged_rec = None
    if text is None:
        p.append("staged record %s: %s" % (dep.record_key, err))
    else:
        try:
            staged_rec = record.parse_record(text)
        except record.RecordParseError as e:
            p.append("staged record cannot be used: %s" % e)
    rep.staged_record = staged_rec
    if staged_rec:
        errors, _ = record.validate_record(staged_rec, vocab_terms, domain_codes)
        for e in errors:
            p.append("staged record invalid: %s" % e)
        if staged_rec.get("identifier") != dep.prefix:
            p.append("staged record identifier %r does not match the deposit's "
                     "prefix %r" % (staged_rec.get("identifier"), dep.prefix))
        if staged_rec.get("dataset_uuid") != dep.dataset_uuid:
            p.append("staged record dataset_uuid does not match the deposit's")
        want = sorted((e["path"], e["checksum_sha256"], e["bytes"]) for e in dep.entries)
        have = sorted((e.get("path"), e.get("checksum_sha256"), e.get("bytes"))
                      for e in staged_rec.get("files", []))
        if want != have:
            p.append("staged record's files manifest differs from the deposit's entries")

    # 3. objects present at size
    for prob in deposit_logic.completion_problems(staged_prefix, dep.entries,
                                                  store.stored_size):
        p.append(prob)
    if p:
        return rep      # no point hashing or label-checking missing objects

    # 4 + 5 + 6. checksums, labels, sizes
    total = 0
    for entry in dep.entries:
        key = staged_prefix + "/" + entry["path"]
        total += entry["bytes"]
        if cfg.max_object_bytes is not None and entry["bytes"] > cfg.max_object_bytes:
            p.append("%s is %d bytes, over the %d-byte object limit"
                     % (entry["path"], entry["bytes"], cfg.max_object_bytes))
        head = client.head_object(Bucket=bucket, Key=key)
        meta = head.get("Metadata", {})
        expected = labels_mod.object_labels(dep.dataset_uuid, entry["checksum_sha256"],
                                            dep.meta["sensitivity"], dep.user)
        for name, value in expected.items():
            if meta.get(name) != value:
                p.append("%s: label %s is %r, expected %r"
                         % (entry["path"], name, meta.get(name), value))
        if verify_checksums:
            actual = _sha256_of_object(client, bucket, key)
            if actual != entry["checksum_sha256"]:
                p.append("%s: stored bytes hash to %s, manifest says %s"
                         % (entry["path"], actual[:12], entry["checksum_sha256"][:12]))
    rep.total_bytes = total
    if cfg.max_deposit_bytes is not None and total > cfg.max_deposit_bytes:
        p.append("deposit totals %d bytes, over the %d-byte limit"
                 % (total, cfg.max_deposit_bytes))

    # 7. member paths (belt and braces)
    for entry in dep.entries:
        err = keys.member_path_error(entry["path"])
        if err:
            p.append("member path: %s" % err)
        if keys.is_reserved_member(entry["path"]):
            p.append("member path %r is reserved" % entry["path"])

    # 8. authorisation
    if not cfg.user_may_deposit_to(dep.user, dep.meta["strand"]):
        p.append("depositor %s is not authorised for strand %s"
                 % (dep.user, dep.meta["strand"]))

    # 9. destination record
    dest_record_key = dep.prefix + "/" + keys.record_filename(dep.meta["dataset"])
    text, err = _read_json_object(client, bucket, dest_record_key)
    if text is not None:
        try:
            existing = record.parse_record(text)
        except record.RecordParseError as e:
            p.append("destination record %s cannot be built on: %s"
                     % (dest_record_key, e))
        else:
            if (existing.get("identifier") != dep.prefix
                    or existing.get("dataset") != dep.meta["dataset"]):
                p.append("destination record's dataset/identifier (%r / %r) do not "
                         "match its location %s - moved or hand-edited"
                         % (existing.get("dataset"), existing.get("identifier"),
                            dep.prefix))
            else:
                rep.existing_record = existing
                _, added, updated = record.merge_manifest(existing.get("files"),
                                                          dep.entries)
                rep.warnings.append(
                    "destination already has dataset %s (%d files, modified %s); "
                    "merging: dataset_uuid %s kept, %d member(s) added, %d updated"
                    % (dep.meta["dataset"], len(existing.get("files", [])),
                       (existing.get("modified") or "?")[:10],
                       existing.get("dataset_uuid", "?")[:8], len(added), len(updated)))
    elif err != "absent":
        p.append("could not read destination record %s: %s" % (dest_record_key, err))

    return rep
