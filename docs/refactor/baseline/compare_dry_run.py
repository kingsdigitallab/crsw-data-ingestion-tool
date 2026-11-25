"""Run the command line's --dry-run on `main` and on this checkout, back
to back, and say whether the two outputs are the same.

Run from the repository root (needs the KCL VPN and rclone, as any dry
run does):

    python docs/refactor/baseline/compare_dry_run.py

`main` is unpacked into a temporary folder with `git archive`; the
checkout itself is not touched. The same sample files, answers and
flags go to both, seconds apart, so the store and the vocabulary are the
same for both runs.
"""
import difflib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
INPUTS = "docs/refactor/baseline"
FLAGS = ["--strand", "rs2", "--project", "csac", "--dataset", "baseline-check",
         "--state", "2_final", "--sensitivity", "green", "--domain", "quant",
         "--provenance", INPUTS + "/prov.json", "--dry-run"]
BASE = sys.argv[1] if len(sys.argv) > 1 else "main"


def dry_run(root):
    env = dict(os.environ, NO_COLOR="1", COLUMNS="100")
    with open(str(Path(root) / INPUTS / "dry-run-answers.txt"), "rb") as answers:
        done = subprocess.run(
            [sys.executable, "deposit.py", INPUTS + "/sample"] + FLAGS,
            cwd=str(root), stdin=answers, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, timeout=180)
    text = done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    return done.returncode, text.split("\n")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(["git", "archive", BASE], cwd=str(ROOT),
                                 stdout=subprocess.PIPE, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp)
        there = Path(tmp) / INPUTS
        there.mkdir(parents=True, exist_ok=True)
        for name in ("prov.json", "dry-run-answers.txt"):
            shutil.copy(str(HERE / name), str(there / name))
        shutil.copytree(str(HERE / "sample"), str(there / "sample"))
        base_code, base_lines = dry_run(tmp)
        here_code, here_lines = dry_run(ROOT)
    print("%s: exit %d, %d lines" % (BASE, base_code, len(base_lines)))
    print("this checkout: exit %d, %d lines" % (here_code, len(here_lines)))
    if base_code == here_code and base_lines == here_lines:
        print("dry run: same")
        return 0
    print("dry run: DIFFERENT")
    for line in difflib.unified_diff(base_lines, here_lines, BASE, "checkout",
                                     lineterm="", n=1):
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main())
