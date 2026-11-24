"""`python -m promoter passages`: rebuild the passages behind search
inside documents, for every record in place or a chosen part."""
import json
import sys

from crsw_web.llm import Platform

from . import audit
from . import index as index_mod
from . import passages as passages_mod
from .cmd_common import _connect, _print_line
from .cmd_derived import _embed_model, _ocr, _passages_skip_reason


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
