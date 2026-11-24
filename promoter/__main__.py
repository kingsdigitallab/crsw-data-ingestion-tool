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

After promotion, `run` also brings every record up to the vocabulary's
own changes when the vocabulary fetched from its repository has moved
since the marker in the bucket (recategorise.read_marker). A record the
replay refuses or cannot write holds the marker back, so the next run
tries again; promotion and the index carry on regardless. A vocabulary
from the cache or the bundled copy may be older and is never replayed.

Exit codes: 0 everything promoted, rewritten or removed (or nothing to
do); 1 at least one deposit or dataset was refused or failed; 2
configuration error, unusable change file, a --confirm that does not
repeat every id, or (a real run) the audit object could not be
written."""
import argparse
import getpass
import json
import sys
from typing import Optional, Tuple

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
from .cmd_common import _whoami, _connect, _setup, _lists, _print_line, _write_audit
from .cmd_derived import _write_index, _embed_if_model_changed, _ocr, _write_passages, _record_in_place, _passages_skip_reason, _embed_model, _refresh_embeddings
from .cmd_recategorise import _apply_plan, _mark_applied, cmd_recategorise
from .cmd_run import _replay_vocabulary, cmd_run


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
    ds.add_argument("--strand", help="only this strand")
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
    ps.add_argument("--strand", help="only this strand")

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
        store = DepositStore(client, cfg.s3_bucket, cfg.staging_prefix)
        terms, codes = vocab.all_terms(vocab_dict), vocab.domain_codes(vocab_dict)
        provider_slugs, place_codes, _ = _lists(vocab_dict)

        def refusal(user, deposit_id):
            """r12: the problems `run` would refuse this deposit for."""
            dep = store.load(user, deposit_id)
            if dep is None:
                return ["control object missing or unreadable"]
            return check(dep, store, cfg, terms, codes, vocab_doc=vocab_dict,
                         provider_slugs=provider_slugs, place_codes=place_codes).problems

        for deposit_id in targets:
            t = remove_mod.inspect_staging(client, cfg.s3_bucket, cfg.staging_prefix,
                                           deposit_id, refusal=refusal)
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
