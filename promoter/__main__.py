"""`python -m promoter run [--dry-run] [--deposit ID] [--user NAME]
[--keep-staging] [--log PATH] [--no-verify-checksums]`

`python -m promoter recategorise [--dry-run] [--changes FILE]
[--dataset ID] [--by NAME] [--log PATH]` (r9): bring dataset records in
place up to the current vocabulary, and apply a steward's change file.

Exit codes: 0 everything promoted or rewritten (or nothing to do); 1 at
least one deposit or dataset was refused or failed; 2 configuration
error or unusable change file."""
import argparse
import getpass
import sys

from crsw_deposit import record, vocab
from crsw_web import s3 as s3mod
from crsw_web.config import load_dotenv
from crsw_web.deposits import DepositStore

from . import recategorise as recat
from .checks import check
from .config import ConfigError, PromoterConfig
from .log import Log
from .promote import promote
from .scan import list_deposits


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="promoter")
    sub = p.add_subparsers(dest="command", required=True)

    def common(s):
        s.add_argument("--dry-run", action="store_true",
                       help="report what would happen; write nothing")
        s.add_argument("--log", help="JSON-lines log path (default PROMOTER_LOG_PATH)")
        s.add_argument("--env-file", default=".env.promoter",
                       help="local env file to load (default .env.promoter)")

    run = sub.add_parser("run", help="check every complete deposit in staging and "
                                     "promote the ones that pass")
    common(run)
    run.add_argument("--deposit", help="only this deposit id")
    run.add_argument("--user", help="only this depositor")
    run.add_argument("--keep-staging", action="store_true",
                     help="promote but leave the staged copy in place")
    run.add_argument("--no-verify-checksums", action="store_true",
                     help="skip re-hashing staged objects (labels are still checked)")

    rc = sub.add_parser("recategorise",
                        help="rewrite dataset records whose categories the "
                             "vocabulary's changes or a change file affect")
    common(rc)
    rc.add_argument("--changes", help="JSON change file: per-dataset add/remove/"
                                      "set_domain decisions")
    rc.add_argument("--dataset", help="only this dataset (identifier or uuid)")
    rc.add_argument("--by", default=None,
                    help="who is making the change (default: your login)")
    return p


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "promoter"


def _setup(args):
    load_dotenv(args.env_file)
    cfg = PromoterConfig.from_env()
    client = s3mod.make_client(cfg)          # same fields as web Settings
    log = Log(args.log or cfg.log_path)
    vocab_dict, source = vocab.load_vocabulary()
    return cfg, client, log, vocab_dict, source


def cmd_run(args) -> int:
    cfg, client, log, vocab_dict, source = _setup(args)
    store = DepositStore(client, cfg.s3_bucket, cfg.staging_prefix)
    terms, codes = vocab.all_terms(vocab_dict), vocab.domain_codes(vocab_dict)
    log.write("start", dry_run=args.dry_run, vocabulary=source,
              vocabulary_version=vocab_dict.get("vocabulary_version"),
              staging=cfg.staging_prefix, bucket=cfg.s3_bucket)

    refused = 0
    promoted = 0
    seen = 0
    for dep in list_deposits(store, user=args.user, deposit_id=args.deposit):
        seen += 1
        rep = check(dep, store, cfg, terms, codes,
                    verify_checksums=not args.no_verify_checksums,
                    vocab_doc=vocab_dict)
        log.write("checked", dep, ok=rep.ok, problems=rep.problems,
                  warnings=rep.warnings, files=len(dep.entries),
                  bytes=rep.total_bytes)
        if not rep.ok:
            refused += 1
            continue
        if args.dry_run:
            log.write("would_promote", dep, dataset_uuid=(
                rep.existing_record["dataset_uuid"] if rep.existing_record
                else dep.dataset_uuid))
            continue
        out = promote(dep, rep, store, log, terms, codes,
                      keep_staging=args.keep_staging)
        if out.promoted:
            promoted += 1
        else:
            refused += 1

    log.write("finish", seen=seen, promoted=promoted, refused_or_failed=refused,
              dry_run=args.dry_run)
    return 1 if refused else 0


def cmd_recategorise(args) -> int:
    cfg, client, log, vocab_dict, source = _setup(args)
    args.by = args.by or _whoami()
    changes = []
    if args.changes:
        try:
            with open(args.changes, encoding="utf-8") as f:
                changes = recat.load_change_file(f.read())
        except OSError as e:
            print("could not read change file: %s" % e, file=sys.stderr)
            return 2
        except recat.ChangeFileError as e:
            print("change file: %s" % e, file=sys.stderr)
            return 2
    log.write("start", command="recategorise", dry_run=args.dry_run,
              vocabulary=source,
              vocabulary_version=vocab_dict.get("vocabulary_version"),
              bucket=cfg.s3_bucket, changes=len(changes), by=args.by)

    plan = recat.plan(client, cfg.s3_bucket, vocab_dict, changes, args.by,
                      record.utc_now_iso(), only=args.dataset)
    for problem in plan.problems:
        log.write("recategorise_refused", problem=problem)
    for item in plan.refused:
        log.write("recategorise_refused", prefix=item.prefix, problems=item.problems)
    rewritten = 0
    failed = 0
    for item in plan.to_write:
        log.write("recategorise_planned", **item.summary())
        if args.dry_run:
            continue
        if recat.apply(client, cfg.s3_bucket, item, log, args.by):
            rewritten += 1
        else:
            failed += 1
    refused = len(plan.problems) + len(plan.refused) + failed
    log.write("finish", command="recategorise", seen=len(plan.items),
              would_rewrite=len(plan.to_write) if args.dry_run else None,
              rewritten=rewritten, refused_or_failed=refused, dry_run=args.dry_run)
    return 1 if refused else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        return cmd_recategorise(args)
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
