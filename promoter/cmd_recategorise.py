"""`python -m promoter recategorise`: bring dataset records in place up to
the current vocabulary, and apply a steward's change file."""
import sys
from typing import Tuple

from crsw_deposit import record

from . import recategorise as recat
from .cmd_common import _lists, _setup, _whoami, _write_audit
from .cmd_derived import _write_index


def _apply_plan(client, cfg, log, plan, who: str, dry_run: bool) -> Tuple[int, int]:
    """Log a recategorise plan and, unless a dry run, write it. Returns
    (rewritten, failed)."""
    for problem in plan.problems:
        log.write("recategorise_refused", problem=problem)
    for item in plan.refused:
        log.write("recategorise_refused", prefix=item.prefix, problems=item.problems)
    rewritten = 0
    failed = 0
    for item in plan.to_write:
        log.write("recategorise_planned", **item.summary())
        if dry_run:
            continue
        if recat.apply(client, cfg.s3_bucket, item, log, who):
            rewritten += 1
        else:
            failed += 1
    return rewritten, failed


def _mark_applied(cfg, client, log, marker, vocab_dict, rewritten: int, **detail) -> None:
    """The whole store is now up to this vocabulary: say so in the bucket
    and on the trail."""
    recat.write_marker(client, cfg.s3_bucket, cfg.index_prefix, vocab_dict,
                       record.utc_now_iso(), log.run_id)
    log.write("vocabulary_replayed", from_version=(marker or {}).get("vocabulary_version"),
              to_version=vocab_dict.get("vocabulary_version"),
              changes=len(vocab_dict.get("changes") or []), rewritten=rewritten, **detail)


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
    provider_slugs, place_codes, places_source = _lists(vocab_dict)
    log.write("start", run_id=log.run_id, command="recategorise", dry_run=args.dry_run,
              vocabulary=source,
              vocabulary_version=vocab_dict.get("vocabulary_version"),
              places=places_source,
              bucket=cfg.s3_bucket, changes=len(changes), by=args.by)

    plan = recat.plan(client, cfg.s3_bucket, vocab_dict, changes, args.by,
                      record.utc_now_iso(), only=args.dataset,
                      provider_slugs=provider_slugs, place_codes=place_codes)
    rewritten, failed = _apply_plan(client, cfg, log, plan, args.by, args.dry_run)
    refused = len(plan.problems) + len(plan.refused) + failed
    if (not args.dry_run and not args.dataset and not refused and source == "remote"
            and cfg.index_prefix):
        # `plan` replays the vocabulary over every record with or without a
        # change file, so the timer need not do it again. If this fails the
        # timer repeats a replay that finds nothing: logged, nothing lost.
        try:
            marker = recat.read_marker(client, cfg.s3_bucket, cfg.index_prefix)
            if not recat.marker_is_current(marker, vocab_dict):
                _mark_applied(cfg, client, log, marker, vocab_dict, rewritten, by=args.by)
        except Exception as e:
            log.write("vocabulary_replay_failed", rewritten=rewritten, reason=str(e))
    _write_index(cfg, client, log, args.dry_run, rewritten, vocab_doc=vocab_dict)
    log.write("finish", command="recategorise", seen=len(plan.items),
              would_rewrite=len(plan.to_write) if args.dry_run else None,
              rewritten=rewritten, refused_or_failed=refused, dry_run=args.dry_run)
    return _write_audit(cfg, client, log, args.dry_run, 1 if refused else 0)
