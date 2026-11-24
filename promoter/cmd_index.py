"""`python -m promoter index`: rebuild the index of every record in place,
and with --embed the embeddings beside it."""
import json
import sys

from crsw_deposit import vocab

from . import index as index_mod
from .cmd_common import _connect, _print_line
from .cmd_derived import _refresh_embeddings


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
