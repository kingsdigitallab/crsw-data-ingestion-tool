"""`python -m promoter run [--dry-run] [--deposit ID] [--user NAME]
[--keep-staging] [--log PATH] [--no-verify-checksums]`

Exit codes: 0 everything promoted (or nothing to do); 1 at least one
deposit was refused or failed; 2 configuration error."""
import argparse
import sys

from crsw_deposit import vocab
from crsw_web import s3 as s3mod
from crsw_web.config import load_dotenv
from crsw_web.deposits import DepositStore

from .checks import check
from .config import ConfigError, PromoterConfig
from .log import Log
from .promote import promote
from .scan import list_deposits


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="promoter")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="check every complete deposit in staging and "
                                     "promote the ones that pass")
    run.add_argument("--dry-run", action="store_true",
                     help="report checks; move nothing")
    run.add_argument("--deposit", help="only this deposit id")
    run.add_argument("--user", help="only this depositor")
    run.add_argument("--keep-staging", action="store_true",
                     help="promote but leave the staged copy in place")
    run.add_argument("--no-verify-checksums", action="store_true",
                     help="skip re-hashing staged objects (labels are still checked)")
    run.add_argument("--log", help="JSON-lines log path (default PROMOTER_LOG_PATH)")
    run.add_argument("--env-file", default=".env.promoter",
                     help="local env file to load (default .env.promoter)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file)
    try:
        cfg = PromoterConfig.from_env()
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2

    client = s3mod.make_client(cfg)          # same fields as web Settings
    store = DepositStore(client, cfg.s3_bucket, cfg.staging_prefix)
    log = Log(args.log or cfg.log_path)
    vocab_dict, source = vocab.load_vocabulary()
    terms, codes = vocab.all_terms(vocab_dict), vocab.domain_codes(vocab_dict)
    log.write("start", dry_run=args.dry_run, vocabulary=source,
              staging=cfg.staging_prefix, bucket=cfg.s3_bucket)

    refused = 0
    promoted = 0
    seen = 0
    for dep in list_deposits(store, user=args.user, deposit_id=args.deposit):
        seen += 1
        rep = check(dep, store, cfg, terms, codes,
                    verify_checksums=not args.no_verify_checksums)
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


if __name__ == "__main__":
    sys.exit(main())
