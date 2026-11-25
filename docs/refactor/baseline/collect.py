"""Write the refactor baseline files beside this script.

Run from the repository root with the project's virtual environment:

    .venv/Scripts/python docs/refactor/baseline/collect.py [OUT_DIR]

Read-only: builds the web app against a mocked store, asks argparse for
help text and imports modules. OUT_DIR defaults to this folder; give
another to compare a later run with the baseline.
"""
import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
os.environ["COLUMNS"] = "100"   # argparse wraps help to the terminal width

PROMOTER_COMMANDS = ("run", "recategorise", "audit", "datasets", "index",
                     "passages", "remove")
NAMES_OF = ("crsw_deposit", "crsw_deposit.keys", "crsw_deposit.record",
            "crsw_deposit.deposit_logic", "crsw_deposit.vocab",
            "crsw_deposit.authority", "crsw_deposit.places",
            "crsw_deposit.labels", "crsw_deposit.noise",
            "crsw_deposit.filetypes", "crsw_deposit.vocab_cli",
            "crsw_web.bulk")


def write(name, text):
    (OUT / name).write_bytes(text.encode("utf-8"))
    print("%-22s %d lines" % (name, text.count("\n")))


# Dummy values only: nothing here reaches a real service.
SWITCHES_ON = dict(read_s3_access_key="dummy", read_s3_secret_key="dummy",
                   llm_base_url="https://llm.example", llm_api_key="dummy",
                   debug_headers=True)


def routes(suffix="", **switches):
    """Route lists for the app built with the test settings plus `switches`."""
    import boto3
    from moto import mock_aws
    from crsw_deposit import places, vocab
    from crsw_web.app import create_app
    from crsw_web.config import Settings
    env = {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
           "AWS_ENDPOINT_URL": "", "AWS_ENDPOINT_URL_S3": ""}
    with mock.patch.dict(os.environ, env), mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
        s3.create_bucket(Bucket="crsw")
        settings = Settings(s3_endpoint="https://rgw.example", s3_access_key="testing",
                            s3_secret_key="testing", s3_bucket="crsw",
                            staging_prefix="staging/_test", **switches)
        vocab_dict = json.loads(Path(vocab.bundled_vocab_path()).read_text(encoding="utf-8"))
        places_dict = json.loads(
            Path(places.bundled_places_path()).read_text(encoding="utf-8"))
        app = create_app(settings, s3_client=s3, vocab_dict=vocab_dict,
                         places_dict=places_dict,
                         read_client=s3 if switches else None)
        registered = []
        for r in app.routes:
            methods = ",".join(sorted(getattr(r, "methods", None) or [])) or "-"
            registered.append("%-12s %s" % (methods, r.path))
        openapi = []
        for path, item in app.openapi()["paths"].items():
            for method in item:
                openapi.append("%-7s %s" % (method.upper(), path))
    write("routes-openapi%s.txt" % suffix, "\n".join(sorted(openapi)) + "\n")
    write("routes-registered%s.txt" % suffix, "\n".join(registered) + "\n")


def promoter_help():
    from promoter import __main__ as prom
    out = []
    for args in [[]] + [[c] for c in PROMOTER_COMMANDS]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            with mock.patch.object(sys, "argv", ["promoter"] + args + ["--help"]):
                prom.main()
        out.append("$ python -m promoter %s--help\n%s" % (
            (args[0] + " ") if args else "", buf.getvalue()))
    write("promoter-help.txt", "\n".join(out))


def public_names():
    out = []
    for modname in NAMES_OF:
        mod = importlib.import_module(modname)
        names = getattr(mod, "__all__", None)
        if names is None:
            names = [n for n in vars(mod) if not n.startswith("_")]
        out.append("[%s]" % modname)
        for n in sorted(names):
            obj = getattr(mod, n)
            kind = type(obj).__name__
            if kind == "module":
                if modname == "crsw_deposit":
                    out.append("  %-10s %s" % (kind, n))
                continue
            if type(obj).__module__ == "typing":
                continue        # List, Optional and the like
            if getattr(obj, "__module__", modname) not in (modname, None) and kind in (
                    "function", "type"):
                continue        # imported from elsewhere, not this module's own
            out.append("  %-10s %s" % (kind, n))
        out.append("")
    write("public-names.txt", "\n".join(out))


if __name__ == "__main__":
    routes()
    routes("-switches-on", **SWITCHES_ON)
    promoter_help()
    public_names()
