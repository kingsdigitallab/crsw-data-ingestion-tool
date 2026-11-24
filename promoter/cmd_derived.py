"""The layers derived from the records, rebuilt after a command has
changed one: the index, the embeddings and the passages."""
import json
from typing import Optional

from crsw_deposit import keys, record
from crsw_web.llm import Platform

from . import embed as embed_mod
from . import index as index_mod
from . import passages as passages_mod


def _write_index(cfg, client, log, dry_run: bool, changed: int,
                 vocab_doc: Optional[dict] = None) -> Optional[dict]:
    """Rewrite the index after a real run that changed a record. Return
    the embeddings summary when vectors were written, else None. Nothing
    is written on a dry run. A run that changed nothing only checks
    whether the embed model or the vocabulary's text has moved. A failure
    is logged and does not change the exit code."""
    if not cfg.index_prefix or dry_run:
        return None
    # Not rewritten when nothing changed: the bucket is versioned, and a
    # write every five minutes would pile up versions.
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
    """Replace embeddings on a run that changed no record, if the model
    behind the embed alias has moved or the vocabulary gives some row
    other text than its vector was made from. Return the summary when
    vectors were written, else None. The rows come from the index in
    place, not from every record."""
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
    """Return the platform's page reader when PROMOTER_LLM_OCR_MODEL names
    an alias, else None."""
    return platform.transcribe if platform is not None and platform.ocr_model else None


def _write_passages(cfg, client, log, prefixes) -> None:
    """Write passages for the datasets a real run promoted. Each dataset
    is its own unit of work and its own log line; a failure is logged and
    the next dataset still runs."""
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
    """Return what this step's vectors are keyed to: the fingerprint of
    what the embed alias resolves to on the platform now, or the alias
    alone when the platform does not say. Also written to the trail."""
    model = platform.embed_fingerprint()
    emit("embed_model_resolved", step=step, alias=cfg.llm_embed_model, model=model)
    return model


def _refresh_embeddings(cfg, client, rows_, emit, only_if_model_changed=False,
                        vocab_doc=None) -> Optional[dict]:
    """Embed what changed, rewrite embeddings.parquet and return the
    summary, or None when nothing was written. Only record metadata goes
    to the platform, with the subject terms' labels and scope notes from
    `vocab_doc`. `emit` writes a log line (or prints, from the command);
    `rows_` may be a callable, read only when there is something to
    write. With `only_if_model_changed`, nothing is written or said unless
    the model behind the alias has moved, a row's text has changed or a
    row is missing."""
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
