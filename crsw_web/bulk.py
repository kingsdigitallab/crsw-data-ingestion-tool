"""The bulk route into staging (r12): a prepared dataset, its record
already built and validated with crsw_deposit.record, written into
staging exactly as the web route would leave it after finalise, so the
promoter needs no new command and applies its usual checks.

It is how the Centre-curated `centre` strand is written (the form and
the CLI do not offer it), and it serves any other prepared dataset too.
boto3 only, no FastAPI, like crsw_web.deposits, so a harvest script can
import it from a sibling checkout.

    python -m crsw_web.bulk --record dataset.x.json --root DIR [--user K]
        [--copy-from PREFIX] [--dry-run]

Members first and the record last (r5 Q4), four labels on every object
including the record, the control object marked complete at the end.
With `copy_from`, a member already in the bucket under that prefix at
the same size is copied server-side and re-labelled instead of being
uploaded; the promoter re-hashes every member, so a stale copy is
caught and reported there, never promoted."""
import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from botocore.exceptions import ClientError

from crsw_deposit import deposit_logic, keys, labels as labels_mod, record

from .deposits import STATUS_COMPLETE, Deposit, DepositStore

UPLOADED = "uploaded"
COPIED = "copied"

# The record fields that are not part of the deposit's metadata.
_NOT_META = ("files", "schema_version", "dataset_uuid", "created", "modified",
             "depositors", "identifier", "temporal")


class BulkError(ValueError):
    """The prepared dataset and the record disagree, or the record cannot
    be staged as it stands. Nothing has been written when this is raised
    from the checks; the message says what to fix."""


class StagingInterrupted(BulkError):
    """The deposit was created but could not be finished (the network, a
    file that vanished): it is open in staging, under `user`, as
    `deposit_id`. It is not promoted; it counts against the depositor's
    staging until `promoter remove --staging ID --confirm ID` takes it
    away, so the id travels with the error."""

    def __init__(self, user: str, deposit_id: str, cause: BaseException):
        self.user, self.deposit_id, self.cause = user, deposit_id, cause
        super().__init__(
            "deposit %s left open in staging under %s after %s: %s; it will not be "
            "promoted: python -m promoter remove --staging %s --confirm %s, then stage again"
            % (deposit_id, user, type(cause).__name__, cause, deposit_id, deposit_id))


@dataclass
class Staged:
    """What stage_dataset did, or with dry_run would do."""
    prefix: str
    members: List[Tuple[str, str, int]] = field(default_factory=list)  # (path, action, bytes)
    deposit: Optional[Deposit] = None   # None on a dry run
    record_key: Optional[str] = None

    @property
    def bytes(self) -> int:
        return sum(b for _, _, b in self.members)

    @property
    def copied(self) -> int:
        return sum(1 for _, a, _ in self.members if a == COPIED)


def meta_from_record(rec: Dict) -> Dict:
    """The deposit metadata the record already carries, in the shape the
    web form's metadata has (validate_meta): the control object holds it,
    and the promoter rebuilds the record from it with
    deposit_logic.assemble_record, so the coverage travels as
    coverage_start/coverage_end, not as the record's `temporal`."""
    meta = {k: v for k, v in rec.items() if k not in _NOT_META}
    meta["coverage_start"], meta["coverage_end"] = record.temporal_pair(rec.get("temporal"))
    return meta


def check_files(rec: Dict, files: Dict[str, Path]) -> None:
    """Refuse when the local set and the manifest differ in paths or
    sizes. The record's checksums are the depositor's; the promoter
    re-hashes the stored bytes against them."""
    manifest = {e["path"]: e for e in rec.get("files") or []}
    if not manifest:
        raise BulkError("the record's files manifest is empty")
    missing = sorted(p for p in manifest if p not in files or not Path(files[p]).is_file())
    extra = sorted(set(files) - set(manifest))
    if missing or extra:
        raise BulkError("local files and the manifest differ: missing locally %s; "
                        "not in the manifest %s" % (missing or "none", extra or "none"))
    wrong = ["%s (manifest %d, local %d)" % (p, manifest[p]["bytes"], Path(files[p]).stat().st_size)
             for p in sorted(manifest)
             if Path(files[p]).stat().st_size != manifest[p]["bytes"]]
    if wrong:
        raise BulkError("local sizes differ from the manifest: %s" % "; ".join(wrong))


def _size_at(client, bucket: str, key: str) -> Optional[int]:
    try:
        return client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def stage_dataset(store: DepositStore, user: str, rec: Dict, files: Dict[str, Path],
                  now: str, copy_from: Optional[str] = None,
                  dry_run: bool = False) -> Staged:
    """Write one prepared dataset into staging/<user>/<deposit-id>/ as a
    complete deposit. `rec` is the record as it will be stored; `files`
    maps each manifest path to its local file. `now` is the deposit's
    creation time (ISO, UTC)."""
    meta = meta_from_record(rec)
    try:
        prefix = deposit_logic.dataset_prefix(meta)
    except (KeyError, ValueError) as e:
        raise BulkError("the record cannot be placed: %s" % e)
    if rec.get("identifier") != prefix:
        raise BulkError("the record's identifier %r does not match its fields (%s)"
                        % (rec.get("identifier"), prefix))
    if user not in (rec.get("depositors") or []):
        raise BulkError("%s is not among the record's depositors %s; the labels and the "
                        "record must name the same person" % (user, rec.get("depositors")))
    check_files(rec, files)
    entries = sorted(rec["files"], key=lambda e: e["path"])
    finals = dict(deposit_logic.plan_keys(meta, [e["path"] for e in entries]))
    client, bucket = store.client, store.bucket
    copy_from = (copy_from or "").strip("/") or None

    out = Staged(prefix=prefix)
    for e in entries:
        source_size = (_size_at(client, bucket, copy_from + "/" + e["path"])
                       if copy_from else None)
        action = COPIED if source_size == e["bytes"] else UPLOADED
        out.members.append((e["path"], action, e["bytes"]))
    if dry_run:
        return out

    dataset_uuid = rec["dataset_uuid"]
    sensitivity = rec["sensitivity"]
    dep = store.create(user, meta, prefix, dataset_uuid, now)
    out.deposit = dep
    try:
        for (path, action, _), e in zip(out.members, entries):
            labels = labels_mod.object_labels(dataset_uuid, e["checksum_sha256"], sensitivity, user)
            staged = store.staged_key(user, dep.id, finals[path])
            if action == COPIED:
                client.copy_object(Bucket=bucket, Key=staged,
                                   CopySource={"Bucket": bucket, "Key": copy_from + "/" + path},
                                   Metadata=labels, MetadataDirective="REPLACE")
            else:
                client.upload_file(str(files[path]), bucket, staged,
                                   ExtraArgs={"Metadata": labels})
            dep.put_entry(dict(e))
        store.save(dep)

        data = deposit_logic.record_bytes(rec)
        checksum = hashlib.sha256(data).hexdigest()
        record_key = store.staged_key(user, dep.id, prefix) + "/" + keys.record_filename(meta["dataset"])
        client.put_object(Bucket=bucket, Key=record_key, Body=data,
                          ContentType="application/json",
                          Metadata=labels_mod.object_labels(dataset_uuid, checksum, sensitivity, user))
        back = client.get_object(Bucket=bucket, Key=record_key)
        record.parse_record(back["Body"].read().decode("utf-8"))

        dep.status = STATUS_COMPLETE
        dep.record_key = record_key
        dep.finalised = rec["modified"]
        store.save(dep)
    except Exception as e:  # the deposit exists now: say so, with its id
        raise StagingInterrupted(user, dep.id, e) from e
    out.record_key = record_key
    return out


def files_under(rec: Dict, root: Path) -> Dict[str, Path]:
    """Each manifest path resolved under a local root (member paths use
    '/', whatever the platform)."""
    return {e["path"]: root.joinpath(*e["path"].split("/")) for e in rec.get("files") or []}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m crsw_web.bulk",
        description="Stage one prepared dataset (record + files) for the promoter.")
    p.add_argument("--record", required=True, type=Path, help="the dataset record (JSON)")
    p.add_argument("--root", required=True, type=Path,
                   help="local folder the manifest's member paths are relative to")
    p.add_argument("--user", help="depositor, a KCL username (default: the record's first)")
    p.add_argument("--copy-from", metavar="PREFIX",
                   help="copy members already in the bucket under PREFIX at the same size")
    p.add_argument("--dry-run", action="store_true", help="say what would be written; write nothing")
    p.add_argument("--env-file", default=".env", help="settings file (default .env)")
    args = p.parse_args(argv)

    # The web settings and client are imported here, not at the top, so a
    # caller of stage_dataset needs neither FastAPI's settings nor httpx.
    from crsw_deposit import places as places_mod
    from crsw_deposit import vocab

    from .config import ConfigError, Settings, load_dotenv
    from .s3 import make_client

    rec = record.parse_record(args.record.read_text(encoding="utf-8"))
    vocab_dict, _ = vocab.load_vocabulary()
    places_doc, _ = places_mod.load_places()
    errors, warnings = record.validate_record(
        rec, vocab.all_terms(vocab_dict), vocab.domain_codes(vocab_dict),
        vocab.activity_codes(vocab_dict), provider_slugs=vocab.provider_slugs(vocab_dict),
        place_codes=places_mod.codes(places_doc))
    for w in warnings:
        print("warning: %s" % w, file=sys.stderr)
    if errors:
        for e in errors:
            print("error: %s" % e, file=sys.stderr)
        return 2
    user = args.user or (rec.get("depositors") or [""])[0]

    load_dotenv(args.env_file)
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    store = DepositStore(make_client(settings), settings.s3_bucket, settings.staging_prefix)
    try:
        out = stage_dataset(store, user, rec, files_under(rec, args.root), record.utc_now_iso(),
                            copy_from=args.copy_from, dry_run=args.dry_run)
    except BulkError as e:
        print(str(e), file=sys.stderr)
        return 2
    for path, action, size in out.members:
        print("%-14s %12d  %s" % (("would " if args.dry_run else "") + action, size, path))
    if out.deposit is None:
        print("dry run: %s, %d members, %d bytes, %d would copy"
              % (out.prefix, len(out.members), out.bytes, out.copied))
    else:
        print("complete: %s deposit %s (%d members, %d bytes, %d copied)"
              % (out.prefix, out.deposit.id, len(out.members), out.bytes, out.copied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
