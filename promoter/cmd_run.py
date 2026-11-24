"""`python -m promoter run`: check every complete deposit in staging,
promote the ones that pass, then bring the records and the derived
layers up to date."""
from typing import Tuple

from crsw_deposit import record, vocab
from crsw_web.deposits import DepositStore
from crsw_web.version import release

from . import index as index_mod
from . import recategorise as recat
from .checks import check
from .cmd_common import _lists, _setup, _write_audit
from .cmd_derived import _write_index, _write_passages
from .cmd_recategorise import _apply_plan, _mark_applied
from .promote import promote
from .scan import list_deposits


def _replay_vocabulary(cfg, client, log, vocab_dict, source, provider_slugs, place_codes,
                       dry_run: bool) -> Tuple[str, int, bool]:
    """Bring every record up to the vocabulary's changes if it has moved
    since the marker. Returns (state, rewritten, failed). Nothing here may
    stop the run: promotion is done and the index still has to be written.
    Only a vocabulary fetched from its repository counts; after a failed
    fetch the cache or bundled copy may be older than the marker and would
    refuse records that already carry newer terms."""
    if not cfg.index_prefix:
        return "off", 0, False               # nowhere to keep the marker
    if source != "remote":
        return "not_remote", 0, False
    rewritten = 0
    try:
        marker = recat.read_marker(client, cfg.s3_bucket, cfg.index_prefix)
        if recat.marker_is_current(marker, vocab_dict):
            return "current", 0, False
        plan = recat.plan(client, cfg.s3_bucket, vocab_dict, [], record.VOCABULARY_ACTOR,
                          record.utc_now_iso(), provider_slugs=provider_slugs,
                          place_codes=place_codes)
        rewritten, failed = _apply_plan(client, cfg, log, plan, record.VOCABULARY_ACTOR,
                                        dry_run)
        trouble = len(plan.problems) + len(plan.refused) + failed
        if dry_run:
            log.write("vocabulary_would_replay",
                      from_version=(marker or {}).get("vocabulary_version"),
                      to_version=vocab_dict.get("vocabulary_version"),
                      would_rewrite=len(plan.to_write), refused_or_failed=trouble)
            return "would_replay", 0, bool(trouble)
        if trouble:
            log.write("vocabulary_replay_failed", rewritten=rewritten,
                      reason="%d record(s) refused or failed; the next run tries again"
                             % trouble)
            return "failed", rewritten, True
        _mark_applied(cfg, client, log, marker, vocab_dict, rewritten)
    except Exception as e:
        log.write("vocabulary_replay_failed", rewritten=rewritten, reason=str(e))
        return "failed", rewritten, True
    return "replayed", rewritten, False


def cmd_run(args) -> int:
    cfg, client, log, vocab_dict, source = _setup(args)
    store = DepositStore(client, cfg.s3_bucket, cfg.staging_prefix)
    terms, codes = vocab.all_terms(vocab_dict), vocab.domain_codes(vocab_dict)
    provider_slugs, place_codes, places_source = _lists(vocab_dict)
    log.write("start", run_id=log.run_id, dry_run=args.dry_run, vocabulary=source,
              vocabulary_version=vocab_dict.get("vocabulary_version"),
              places=places_source, staging=cfg.staging_prefix, bucket=cfg.s3_bucket,
              release=release())

    refused = 0
    promoted = 0
    promoted_prefixes = []
    seen = 0
    for dep in list_deposits(store, user=args.user, deposit_id=args.deposit):
        seen += 1
        rep = check(dep, store, cfg, terms, codes,
                    verify_checksums=not args.no_verify_checksums,
                    vocab_doc=vocab_dict, provider_slugs=provider_slugs,
                    place_codes=place_codes)
        log.write("checked", dep, ok=rep.ok, problems=rep.problems,
                  warnings=rep.warnings, files=len(dep.entries),
                  bytes=rep.total_bytes)
        for r in rep.resolved_references:
            # r8 §4: what the store knows about the parent, filled in.
            log.write("resolved_reference", dep, identifier=r["identifier"],
                      dataset_uuid=r["dataset_uuid"], version=r["version"],
                      dry_run=args.dry_run)
        for u in rep.unresolved_references:
            log.write("reference_unresolved", dep, identifier=u["identifier"],
                      reason=u["reason"])
        if not rep.ok:
            refused += 1
            continue
        if args.dry_run:
            log.write("would_promote", dep, dataset_uuid=(
                rep.existing_record["dataset_uuid"] if rep.existing_record
                else dep.dataset_uuid))
            continue
        out = promote(dep, rep, store, log, terms, codes,
                      keep_staging=args.keep_staging,
                      activity_codes=vocab.activity_codes(vocab_dict),
                      provider_slugs=provider_slugs, place_codes=place_codes)
        if out.promoted:
            promoted += 1
            promoted_prefixes.append(dep.prefix)
        else:
            refused += 1

    if args.user or args.deposit:
        # A narrowed run is about those deposits, not the whole store.
        replay_state, replayed, replay_failed = "skipped", 0, False
    else:
        replay_state, replayed, replay_failed = _replay_vocabulary(
            cfg, client, log, vocab_dict, source, provider_slugs, place_codes, args.dry_run)
    # Replay changes records, never files: the passages are not touched.
    summary = _write_index(cfg, client, log, args.dry_run, promoted + replayed,
                           vocab_doc=vocab_dict)
    if summary and summary.get("model_changed"):
        # Every passage in place is from the old model too: sweep them
        # all, not only what this run promoted.
        promoted_prefixes = [ref.prefix for ref, rec, _ in
                             index_mod.read_records(client, cfg.s3_bucket) if rec]
    _write_passages(cfg, client, log, promoted_prefixes)
    log.write("finish", seen=seen, promoted=promoted, refused_or_failed=refused,
              vocabulary=replay_state, replayed=replayed, dry_run=args.dry_run)
    return _write_audit(cfg, client, log, args.dry_run, 1 if refused or replay_failed else 0)
