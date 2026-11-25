"""`python -m promoter remove`: remove a dataset from the store, or an
abandoned deposit from staging."""
import sys

from crsw_deposit import vocab
from crsw_web.deposits import DepositStore

from . import index as index_mod
from . import passages as passages_mod
from . import remove as remove_mod
from .checks import check
from .cmd_common import _lists, _setup, _whoami, _write_audit
from .cmd_derived import _write_index


def cmd_remove(args) -> int:
    """Run `promoter remove` and return the exit code. Without --confirm
    repeating every id it logs the plan and touches nothing. Each target
    is its own unit of work with its own log lines; the derived layers
    are rebuilt once at the end."""
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
        """Handle one target: refused, planned or removed. Return True when
        it was removed."""
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
            """Return the problems `run` would refuse this deposit for."""
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
