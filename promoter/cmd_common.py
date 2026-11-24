"""What every promoter command starts and ends with: who is running it,
the connection to the store, the vocabulary and the lists records are
checked against, a printed JSON line, and the audit object written
before exit."""
import getpass
import json

from crsw_deposit import places as places_mod, vocab
from crsw_web import s3 as s3mod
from crsw_web.config import load_dotenv

from . import audit
from .config import PromoterConfig
from .log import Log


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
