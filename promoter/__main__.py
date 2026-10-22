"""`python -m promoter run [--dry-run] [--deposit ID] [--user NAME]
[--keep-staging] [--log PATH] [--no-verify-checksums]`

`python -m promoter recategorise [--dry-run] [--changes FILE]
[--dataset ID] [--by NAME] [--log PATH]` (r9): bring dataset records in
place up to the current vocabulary, and apply a steward's change file.

`python -m promoter audit [--runs N | --since DATE] [--dataset ID]
[--user NAME] [--action NAME] [--json]`: read the audit trail back from
the bucket. `python -m promoter datasets [--strand rsN] [--json]`: every
dataset record in place. `python -m promoter index`: rebuild the index
of every record in place (datasets.jsonl and datasets.parquet under
PROMOTER_INDEX_PREFIX); run and recategorise rewrite it themselves
after any real run that promoted or rewrote a record. With
PROMOTER_LLM_BASE_URL and PROMOTER_LLM_API_KEY set, those runs also
refresh embeddings.parquet beside the index (one vector per record of
the sensitivities PROMOTER_LLM_SENSITIVITIES allows, green by default);
`python -m promoter index --embed` does the same by hand. With
PROMOTER_PASSAGES=1 as well, a promotion also reads the dataset's
text-bearing files, splits them into passages and embeds those
(`index/passages/<identifier>.parquet`, finding-and-reuse.md §7);
`python -m promoter passages [--dataset ID | --strand rsN]` rebuilds
them for every record in place.

A deposit whose derived_from names another dataset in the store has the
parent's uuid and version filled in before promotion (r8 §4): logged as
resolved_reference, or reference_unresolved when the parent is absent
(a warning; the deposit still promotes with the reference as typed).

`python -m promoter remove --dataset ID [--dataset ID ...] [--confirm ID
...] [--reason TEXT] [--by NAME]` (r10): a dataset leaves the store only
this way. Every object version under the prefix is deleted, members
first and the record last, then the index, embeddings and passages
follow; a dry run (the plan, as would_remove and remaining lines)
unless --confirm repeats every identifier. `--staging DEPOSIT_ID` does
the same for an abandoned deposit in staging, control object last and
nothing derived to rebuild.

Every run, recategorise and remove writes its log lines as one object
under PROMOTER_AUDIT_PREFIX before it exits (dry runs too, marked as
such).

Exit codes: 0 everything promoted, rewritten or removed (or nothing to
do); 1 at least one deposit or dataset was refused or failed; 2
configuration error, unusable change file, a --confirm that does not
repeat every id, or (a real run) the audit object could not be
written."""
import argparse
import getpass
import json
import sys
from typing import Optional

from crsw_deposit import keys, places as places_mod, record, vocab
from crsw_web import s3 as s3mod
from crsw_web.config import load_dotenv
from crsw_web.deposits import DepositStore
from crsw_web.llm import Platform
from crsw_web.version import release

from . import audit
from . import embed as embed_mod
from . import index as index_mod
from . import passages as passages_mod
from . import recategorise as recat
from . import remove as remove_mod
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
                                      "set_domain decisions, and fields to set "
                                      "in place (r11)")
    rc.add_argument("--dataset", help="only this dataset (identifier or uuid)")
    rc.add_argument("--by", default=None,
                    help="who is making the change (default: your login)")

    au = sub.add_parser("audit", help="read the audit trail back from the bucket")
    au.add_argument("--env-file", default=".env.promoter",
                    help="local env file to load (default .env.promoter)")
    au.add_argument("--runs", type=int, default=10,
                    help="newest N runs (default 10; 0 = all)")
    au.add_argument("--since", help="every run on or after this day, YYYY-MM-DD")
    au.add_argument("--dataset", help="only lines about this dataset (prefix, "
                                      "dataset name or uuid)")
    au.add_argument("--user", help="only lines about this depositor or steward")
    au.add_argument("--action", help="only this action, e.g. promoted, checked, "
                                     "record_rewritten, resolved_reference")
    au.add_argument("--json", action="store_true", help="raw JSON lines")

    ds = sub.add_parser("datasets", help="every dataset record in place")
    ds.add_argument("--env-file", default=".env.promoter",
                    help="local env file to load (default .env.promoter)")
    ds.add_argument("--strand", help="only this strand (rs1-rs4)")
    ds.add_argument("--json", action="store_true", help="one JSON object per line")

    ix = sub.add_parser("index", help="rebuild the index of every record in place")
    ix.add_argument("--env-file", default=".env.promoter",
                    help="local env file to load (default .env.promoter)")
    ix.add_argument("--embed", action="store_true",
                    help="also refresh embeddings.parquet through the LLM platform "
                         "(needs PROMOTER_LLM_BASE_URL and PROMOTER_LLM_API_KEY)")

    ps = sub.add_parser("passages", help="rebuild the passages behind search inside "
                                         "documents (needs PROMOTER_PASSAGES=1)")
    ps.add_argument("--env-file", default=".env.promoter",
                    help="local env file to load (default .env.promoter)")
    ps.add_argument("--dataset", help="only this dataset identifier")
    ps.add_argument("--strand", help="only this strand (rs1-rs4)")

    rm = sub.add_parser("remove", help="remove a dataset (every object version under its "
                                       "prefix) or an abandoned staging deposit; the plan "
                                       "only, unless --confirm repeats every id")
    what = rm.add_mutually_exclusive_group(required=True)
    what.add_argument("--dataset", action="append", default=[], metavar="ID",
                      help="a dataset identifier (strand/project/sensitivity/state/"
                           "dataset); repeat for several. No wildcards.")
    what.add_argument("--staging", action="append", default=[], metavar="DEPOSIT_ID",
                      help="an abandoned deposit in staging (no record written)")
    rm.add_argument("--confirm", action="append", default=[], metavar="ID",
                    help="repeat every --dataset or --staging id to do it; without, "
                         "the plan is printed and nothing is touched")
    rm.add_argument("--reason", help="why, for the audit line")
    rm.add_argument("--by", default=None,
                    help="who is removing it (default: your login)")
    rm.add_argument("--log", help="JSON-lines log path (default PROMOTER_LOG_PATH)")
    rm.add_argument("--env-file", default=".env.promoter",
                    help="local env file to load (default .env.promoter)")
    return p


def _whoami() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "promoter"


def _connect(args):
    load_dotenv(args.env_file)
    cfg = PromoterConfig.from_env()
    client = s3mod.make_client(cfg)          # same fields as web Settings
    return cfg, client


def _setup(args):
    cfg, client = _connect(args)
    log = Log(args.log or cfg.log_path)
    vocab_dict, source = vocab.load_vocabulary()
    return cfg, client, log, vocab_dict, source


def _lists(vocab_dict):
    """r11: what provider slugs and place codes are checked against:
    the vocabulary's providers and places.json through its own chain.
    Returns (provider_slugs, place_codes, places_source)."""
    places_doc, places_source = places_mod.load_places()
    return vocab.provider_slugs(vocab_dict), places_mod.codes(places_doc), places_source


def _write_index(cfg, client, log, dry_run: bool, changed: int,
                 vocab_doc: Optional[dict] = None) -> Optional[dict]:
    """Rewrite the index after a real run that changed a record. Never
    on a dry run, never when nothing changed (the bucket is versioned;
    a write every five minutes would pile up versions). A failure is
    logged and does not change the exit code: the index is a cache
    that `promoter index` rebuilds. A run that changed nothing still
    asks what the embed alias resolves to today, and replaces every
    vector when that moved. Returns the embeddings summary when vectors
    were written, else None."""
    if not cfg.index_prefix or dry_run:
        return None
    if not changed:
        return _embed_if_model_changed(cfg, client, log, vocab_doc)
    try:
        rows_ = index_mod.rows(client, cfg.s3_bucket)
        keys_ = index_mod.write(client, cfg.s3_bucket, cfg.index_prefix, rows_)
    except Exception as e:
        log.write("index_write_failed", error=str(e))
        return None
    log.write("index_written", datasets=len(rows_), **keys_)
    if keys_["parquet"] is None:
        log.write("index_parquet_skipped", reason="pyarrow is not installed")
    if not cfg.embed_enabled:
        return None
    try:
        summary = _refresh_embeddings(cfg, client, rows_, log.write, vocab_doc=vocab_doc)
    except Exception as e:
        log.write("embeddings_failed", error=str(e))
        return None
    if summary:
        log.write("embeddings_written", **summary)
    return summary


def _embed_if_model_changed(cfg, client, log, vocab_doc: Optional[dict] = None
                            ) -> Optional[dict]:
    """The quiet run's check: one GET for the fingerprint, one for the
    previous file, one for the index; nothing more unless the backend
    behind the alias has moved (every vector in place is from another
    model and is replaced now rather than at the next promotion) or the
    vocabulary of the day gives some row other text than its vector was
    made from (a scope note or label edited: those rows are remade).
    The rows come from the index in place, not from every record."""
    if not cfg.embed_enabled:
        return None
    try:
        summary = _refresh_embeddings(cfg, client,
                                      lambda: index_mod.read_rows(client, cfg.s3_bucket,
                                                                  cfg.index_prefix),
                                      log.write, only_if_model_changed=True,
                                      vocab_doc=vocab_doc)
    except Exception as e:
        log.write("embeddings_failed", error=str(e))
        return None
    if summary:
        log.write("embeddings_written", **summary)
    return summary


def _ocr(platform):
    """The platform's page reader when PROMOTER_LLM_OCR_MODEL names an
    alias, else None: a PDF with no text layer then keeps its marker."""
    return platform.transcribe if platform is not None and platform.ocr_model else None


def _write_passages(cfg, client, log, prefixes) -> None:
    """After a real run: passages for the datasets it promoted. Each
    dataset is its own unit of work and its own log line; a failure is
    logged and the next dataset still runs."""
    if not cfg.passages_enabled or not prefixes:
        return
    platform = Platform.from_settings(cfg)
    try:
        model = _embed_model(cfg, platform, log.write, "passages")
        for prefix in prefixes:
            rec = _record_in_place(client, cfg.s3_bucket, prefix)
            if rec is None:
                log.write("passages_skipped", prefix=prefix, reason="no record in place")
                continue
            reason = _passages_skip_reason(cfg, prefix, rec)
            if reason:
                try:
                    key = passages_mod.remove(client, cfg.s3_bucket, cfg.index_prefix, prefix)
                except Exception as e:
                    log.write("passages_failed", prefix=prefix, error=str(e))
                    continue
                if key:
                    log.write("passages_removed", prefix=prefix, key=key, reason=reason)
                else:
                    log.write("passages_skipped", prefix=prefix, reason=reason)
                continue
            try:
                s = passages_mod.refresh(client, cfg.s3_bucket, cfg.index_prefix, prefix, rec,
                                         platform, model, cfg.llm_embed_dims or None,
                                         cfg.passages_max_file_bytes,
                                         cfg.passages_max_per_dataset,
                                         words=cfg.passage_words, overlap=cfg.passage_overlap,
                                         ocr=_ocr(platform), ocr_pages=cfg.ocr_pages)
            except Exception as e:
                log.write("passages_failed", prefix=prefix, error=str(e))
                continue
            log.write("passages_written", **s.as_dict())
    finally:
        platform.close()


def _record_in_place(client, bucket, prefix):
    key = prefix + "/" + keys.record_filename(prefix.rsplit("/", 1)[-1])
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        rec, _ = record.upgrade_record(json.loads(body.decode("utf-8")))
        return rec
    except Exception:
        return None


def _passages_skip_reason(cfg, prefix, rec):
    if rec.get("sensitivity") not in cfg.llm_sensitivities:
        return "sensitivity %s not in PROMOTER_LLM_SENSITIVITIES" % rec.get("sensitivity")
    if passages_mod.excluded(prefix, cfg.passages_exclude):
        return "excluded by PROMOTER_PASSAGES_EXCLUDE"
    return None


def _embed_model(cfg, platform, emit, step: str) -> str:
    """What the vectors of this step are keyed to: the fingerprint of
    what the embed alias resolves to on the platform right now (or the
    alias alone when the platform does not say). Said on the trail, so
    the audit shows what made the vectors and when it changed."""
    model = platform.embed_fingerprint()
    emit("embed_model_resolved", step=step, alias=cfg.llm_embed_model, model=model)
    return model


def _refresh_embeddings(cfg, client, rows_, emit, only_if_model_changed=False,
                        vocab_doc=None) -> Optional[dict]:
    """Embed what changed and rewrite embeddings.parquet. Only record
    metadata goes to the platform (promoter/embed.py, text_for), with
    the subject terms' labels and scope notes from `vocab_doc`. `emit`
    is the run's log line writer (or print, from the command); `rows_`
    may be a callable, read only when there is something to write. A
    previous file keyed to another fingerprint is said on the trail
    (`embed_model_changed`); with `only_if_model_changed` that, or
    rows whose text the vocabulary has since changed
    (`embed_text_changed`), are the only cases anything is written or
    said, else None."""
    platform = Platform.from_settings(cfg)
    try:
        model = platform.embed_fingerprint()
        if "|" not in model:
            # GET /models failed or named no backend for the alias, so
            # the fingerprint is the alias alone. Vectors keyed to it
            # would be replaced wholesale by the next run that resolves,
            # so nothing is written now; the datasets left without a row
            # are embedded then (embed_rows_missing).
            emit("embed_model_unresolved", step="embeddings", alias=cfg.llm_embed_model,
                 model=model,
                 hint="GET /models failed or lists no backend for the alias; check "
                      "PROMOTER_LLM_BASE_URL, the key and the route to the platform")
            return None
        existing = embed_mod.read_existing(client, cfg.s3_bucket,
                                           embed_mod.embeddings_key(cfg.index_prefix))
        previous = embed_mod.models_in(existing)
        changed = bool(previous) and previous != [model]
        stale, missing = [], []
        if only_if_model_changed and not changed:
            rows_ = rows_() if callable(rows_) else rows_
            stale = embed_mod.stale_text(rows_, existing, vocab_doc)
            missing = embed_mod.missing_rows(rows_, existing)
            if not stale and not missing:
                return None              # the quiet run has nothing to say
        emit("embed_model_resolved", step="embeddings", alias=cfg.llm_embed_model, model=model)
        if changed:
            emit("embed_model_changed", previous=previous, model=model)
        if stale:
            emit("embed_text_changed", datasets=len(stale))
        if missing:
            emit("embed_rows_missing", datasets=len(missing))
        if callable(rows_):
            rows_ = rows_()
        summary = embed_mod.refresh(client, cfg.s3_bucket, cfg.index_prefix, rows_,
                                    platform, model, set(cfg.llm_sensitivities),
                                    cfg.llm_embed_dims or None, existing=existing,
                                    vocab_doc=vocab_doc)
    finally:
        platform.close()
    summary["model"] = model
    summary["model_changed"] = changed
    return summary


def _print_line(action: str, **detail) -> None:
    print(json.dumps({"action": action, **detail}, ensure_ascii=False))


def _write_audit(cfg, client, log, dry_run: bool, code: int) -> int:
    """Keep this run's lines in the bucket. A real run whose audit
    object cannot be written exits 2: a promotion without its trail is
    the one thing that must be noticed."""
    if not cfg.audit_prefix:
        return code
    try:
        key = audit.write_run(client, cfg.s3_bucket, cfg.audit_prefix, log)
    except Exception as e:
        log.write("audit_write_failed", error=str(e), dry_run=dry_run)
        return code if dry_run else 2
    log.write("audit_written", key=key)
    return code


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

    summary = _write_index(cfg, client, log, args.dry_run, promoted, vocab_doc=vocab_dict)
    if summary and summary.get("model_changed"):
        # Every passage in place is from the old model too: sweep them
        # all, not only what this run promoted.
        promoted_prefixes = [ref.prefix for ref, rec, _ in
                             index_mod.read_records(client, cfg.s3_bucket) if rec]
    _write_passages(cfg, client, log, promoted_prefixes)
    log.write("finish", seen=seen, promoted=promoted, refused_or_failed=refused,
              dry_run=args.dry_run)
    return _write_audit(cfg, client, log, args.dry_run, 1 if refused else 0)


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
    _write_index(cfg, client, log, args.dry_run, rewritten, vocab_doc=vocab_dict)
    log.write("finish", command="recategorise", seen=len(plan.items),
              would_rewrite=len(plan.to_write) if args.dry_run else None,
              rewritten=rewritten, refused_or_failed=refused, dry_run=args.dry_run)
    return _write_audit(cfg, client, log, args.dry_run, 1 if refused else 0)


def cmd_remove(args) -> int:
    """r10: a dataset leaves the store only this way. The plan unless
    --confirm repeats every id; each dataset its own unit of work and its
    own lines; the derived layers rebuilt once at the end, as after a
    run that changed a record."""
    targets = list(dict.fromkeys(args.dataset or args.staging))
    dry_run = not args.confirm
    if not dry_run and sorted(set(args.confirm)) != sorted(targets):
        print("remove: --confirm must repeat every --dataset identifier (or --staging "
              "id) exactly, one --confirm each; nothing done", file=sys.stderr)
        return 2
    cfg, client, log, vocab_dict, _source = _setup(args)
    args.by = args.by or _whoami()
    mode = "dataset" if args.dataset else "staging"
    log.write("start", run_id=log.run_id, command="remove", dry_run=dry_run,
              bucket=cfg.s3_bucket, by=args.by, reason=args.reason, mode=mode,
              targets=targets)

    removed, refused, removed_ids = 0, 0, []

    def do(t, name, would, done, **where):
        """One target: refused, planned, or removed; True when removed."""
        nonlocal removed, refused
        if t.problems:
            log.write("remove_refused", problems=t.problems, **where)
            refused += 1
            return False
        log.write(would, **where, **t.detail)
        if dry_run:
            return False
        try:
            remove_mod.remove(client, cfg.s3_bucket, t)
        except remove_mod.RemoveIncomplete as e:
            log.write("remove_incomplete", left=e.left, **where)
            refused += 1
            return False
        except Exception as e:
            log.write("remove_failed", error=str(e), **where)
            refused += 1
            return False
        log.write(done, **where, **t.detail, reason=args.reason, by=args.by)
        removed += 1
        removed_ids.append(name)
        return True

    if mode == "dataset":
        index_rows = index_mod.rows(client, cfg.s3_bucket)
        for identifier in targets:
            t = remove_mod.inspect(client, cfg.s3_bucket, identifier, index_rows)
            strand = identifier.split("/")[0]
            if not t.problems and not cfg.user_may_deposit_to(args.by, strand):
                # The same policy as a deposit, in reverse.
                t.problems.append("PROMOTER_AUTHORISED would not let %s deposit to %s"
                                  % (args.by, strand))
            do(t, identifier, "would_remove", "dataset_removed", prefix=identifier)
        remaining = [r["identifier"] for r in index_rows if r["identifier"] not in targets]
        log.write("remaining", datasets=remaining, count=len(remaining))
        if removed:
            _write_index(cfg, client, log, False, removed, vocab_doc=vocab_dict)
            for identifier in (removed_ids if cfg.index_prefix else []):
                # No platform call: the file is simply gone with its dataset.
                try:
                    key = passages_mod.remove(client, cfg.s3_bucket, cfg.index_prefix,
                                              identifier)
                except Exception as e:
                    log.write("passages_failed", prefix=identifier, error=str(e))
                    continue
                if key:
                    log.write("passages_removed", prefix=identifier, key=key,
                              reason="no record in place")
    else:
        for deposit_id in targets:
            t = remove_mod.inspect_staging(client, cfg.s3_bucket, cfg.staging_prefix,
                                           deposit_id)
            do(t, deposit_id, "would_remove_staging", "staging_removed", deposit=deposit_id)

    log.write("finish", command="remove", targets=len(targets), removed=removed,
              refused_or_failed=refused, dry_run=dry_run, nothing_done=dry_run)
    return _write_audit(cfg, client, log, dry_run, 1 if refused else 0)


def cmd_index(args) -> int:
    cfg, client = _connect(args)
    if not cfg.index_prefix:
        print("PROMOTER_INDEX_PREFIX is blank: no index is kept", file=sys.stderr)
        return 2
    rows_ = index_mod.rows(client, cfg.s3_bucket)
    keys_ = index_mod.write(client, cfg.s3_bucket, cfg.index_prefix, rows_)
    print(json.dumps({"action": "index_written", "datasets": len(rows_), **keys_},
                     ensure_ascii=False))
    if keys_["parquet"] is None:
        print("pyarrow is not installed: only the JSON lines index was written",
              file=sys.stderr)
    if args.embed:
        if not cfg.embed_enabled:
            print("PROMOTER_LLM_BASE_URL, PROMOTER_LLM_API_KEY and PROMOTER_LLM_EMBED_MODEL "
                  "must all be set for embeddings", file=sys.stderr)
            return 2
        # The vocabulary of the day: its labels and scope notes are in the text.
        vocab_dict, source = vocab.load_vocabulary()
        _print_line("vocabulary", source=source,
                    version=vocab_dict.get("vocabulary_version"))
        summary = _refresh_embeddings(cfg, client, rows_, _print_line, vocab_doc=vocab_dict)
        if summary is None:              # embed_model_unresolved was printed
            return 1
        _print_line("embeddings_written", **summary)
        if summary["key"] is None:
            print("pyarrow is not installed: embeddings were not written", file=sys.stderr)
    return 0


def cmd_audit(args) -> int:
    cfg, client = _connect(args)
    if not cfg.audit_prefix:
        print("PROMOTER_AUDIT_PREFIX is blank: no audit trail is kept", file=sys.stderr)
        return 2
    entries = audit.trail(client, cfg.s3_bucket, cfg.audit_prefix,
                          runs=(None if args.runs == 0 else args.runs),
                          since=args.since, dataset=args.dataset,
                          user=args.user, action=args.action)
    n = 0
    for e in entries:
        n += 1
        print(json.dumps(e, ensure_ascii=False) if args.json else audit.describe(e))
    if n == 0 and not args.json:
        print("(no matching audit lines)")
    return 0


def cmd_datasets(args) -> int:
    cfg, client = _connect(args)
    try:
        strand = audit.valid_strand(args.strand)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    rows = audit.list_records(client, cfg.s3_bucket, strand=strand)
    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(audit.table(rows))
    return 0


def cmd_passages(args) -> int:
    cfg, client = _connect(args)
    if not cfg.passages_enabled:
        print("PROMOTER_PASSAGES=1 plus PROMOTER_LLM_BASE_URL, PROMOTER_LLM_API_KEY, "
              "PROMOTER_LLM_EMBED_MODEL and a PROMOTER_INDEX_PREFIX are needed for passages",
              file=sys.stderr)
        return 2
    try:
        audit.valid_strand(args.strand)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    failed = 0
    totals = {"datasets": 0, "skipped": 0, "removed": 0, "passages": 0, "embedded": 0, "kept": 0,
              "files_no_text": 0, "files_ocr": 0, "pages_ocr": 0, "files_other": 0,
              "files_too_big": 0}
    # One listing of the passages files in scope up front: a dataset no
    # longer permitted is checked against it, and a file whose dataset is
    # gone is removed after the walk.
    try:
        existing = passages_mod.listing(client, cfg.s3_bucket, cfg.index_prefix,
                                        strand=args.strand or "", dataset=args.dataset or "")
    except Exception as e:
        print(json.dumps({"action": "passages_failed", "prefix": None,
                          "error": "passages listing failed: %s" % e}, ensure_ascii=False))
        print(json.dumps({"action": "passages_finished", "failed": 1, **totals},
                         ensure_ascii=False))
        return 1
    platform = Platform.from_settings(cfg)
    model = _embed_model(cfg, platform, _print_line, "passages")

    def in_scope(identifier):
        return ((not args.dataset or identifier == args.dataset) and
                (not args.strand or identifier.startswith(args.strand + "/")))

    def remove(identifier, key, reason):
        """Delete a passages file that is no longer permitted; True if done."""
        try:
            client.delete_object(Bucket=cfg.s3_bucket, Key=key)
        except Exception as e:
            print(json.dumps({"action": "passages_failed", "prefix": identifier,
                              "error": str(e)}, ensure_ascii=False))
            return False
        totals["removed"] += 1
        print(json.dumps({"action": "passages_removed", "prefix": identifier, "key": key,
                          "reason": reason}, ensure_ascii=False))
        return True

    walked = set()
    try:
        for ref, rec, _labels in index_mod.read_records(client, cfg.s3_bucket, args.strand):
            if args.dataset and ref.prefix != args.dataset:
                continue
            walked.add(ref.prefix)
            if not rec:
                totals["skipped"] += 1
                print(json.dumps({"action": "passages_skipped", "prefix": ref.prefix,
                                  "reason": "record unreadable"}, ensure_ascii=False))
                continue
            reason = _passages_skip_reason(cfg, ref.prefix, rec)
            if reason:
                # No longer permitted: passages built before must not linger.
                totals["skipped"] += 1
                if ref.prefix in existing:
                    if not remove(ref.prefix, existing[ref.prefix], reason):
                        failed += 1
                else:
                    print(json.dumps({"action": "passages_skipped", "prefix": ref.prefix,
                                      "reason": reason}, ensure_ascii=False))
                continue
            try:
                s = passages_mod.refresh(client, cfg.s3_bucket, cfg.index_prefix, ref.prefix,
                                         rec, platform, model, cfg.llm_embed_dims or None,
                                         cfg.passages_max_file_bytes,
                                         cfg.passages_max_per_dataset,
                                         words=cfg.passage_words, overlap=cfg.passage_overlap,
                                         ocr=_ocr(platform), ocr_pages=cfg.ocr_pages)
            except Exception as e:
                failed += 1
                print(json.dumps({"action": "passages_failed", "prefix": ref.prefix,
                                  "error": str(e)}, ensure_ascii=False))
                continue
            totals["datasets"] += 1
            for k in ("passages", "embedded", "kept", "files_no_text", "files_ocr", "pages_ocr",
                      "files_other", "files_too_big"):
                totals[k] += getattr(s, k)
            print(json.dumps({"action": "passages_written", **s.as_dict()}, ensure_ascii=False))
        for identifier, key in sorted(existing.items()):
            if identifier not in walked and in_scope(identifier):
                if not remove(identifier, key, "no record in place"):
                    failed += 1
    finally:
        platform.close()
    print(json.dumps({"action": "passages_finished", "failed": failed, **totals},
                     ensure_ascii=False))
    return 1 if failed else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"run": cmd_run, "recategorise": cmd_recategorise,
                "audit": cmd_audit, "datasets": cmd_datasets,
                "index": cmd_index, "passages": cmd_passages,
                "remove": cmd_remove}[args.command](args)
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
