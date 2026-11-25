"""Compare validate_record and validate_authority on this checkout with
the same two functions on another commit (default `main`), over many
deliberately damaged inputs.

Run from the repository root; standard library only:

    python docs/refactor/compare_validators.py [BASE]

The other commit's crsw_deposit is unpacked into a temporary folder
under another name. The same damaged records and vocabularies, from a
fixed random seed, go to both; errors, warnings and exceptions must be
identical and in the same order. Exit code 1 if any case differs.
"""
import copy
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
BASE_REF = sys.argv[1] if len(sys.argv) > 1 else "main"
sys.path.insert(0, str(repo))
from crsw_deposit import authority as new_auth, record as new_rec  # noqa: E402

tmp = Path(tempfile.mkdtemp())
pkg = tmp / "old_crsw_deposit"
pkg.mkdir()
for name in subprocess.run(["git", "ls-tree", "--name-only", BASE_REF, "crsw_deposit/"],
                           cwd=str(repo), capture_output=True, check=True
                           ).stdout.decode().split():
    data = subprocess.run(["git", "show", BASE_REF + ":" + name], cwd=str(repo),
                          capture_output=True, check=True).stdout
    (pkg / Path(name).name).write_bytes(data)
sys.path.insert(0, str(tmp))
import old_crsw_deposit  # noqa: E402,F401
from old_crsw_deposit import authority as old_auth, record as old_rec  # noqa: E402

assert "old_crsw_deposit" in old_rec.__file__ and "old_crsw_deposit" not in new_rec.__file__

vocab = json.loads((repo / "crsw_deposit" / "vocab.json").read_text(encoding="utf-8"))
terms = {t["slug"] for t in vocab["terms"] if t.get("status") == "current"}
SHA = "a" * 64
BASE = {
    "schema_version": "0.7", "dataset_uuid": "3f2b8c1e-4a5d-4e6f-8a9b-0c1d2e3f4a5b",
    "identifier": "rs2/csac/green/2_final/x", "strand": "rs2", "domain": ["quant"],
    "project": "csac", "dataset": "x", "state": "2_final", "sensitivity": "green",
    "version": "1-0", "title": "t", "abstract": "word " * 60,
    "subject": [sorted(terms)[0]], "temporal": {"start": "2020", "end": "2021"},
    "depositors": ["k1"], "created": "2026-01-01T00:00:00Z",
    "modified": "2026-01-01T00:00:00Z", "license": "CC-BY-4.0",
    "source_type": "derived", "source_detail": "somewhere specific",
    "creator": "c", "steward": "s", "notes": "n", "language": ["en"],
    "provider": ["acled"], "spatial": ["m49:004", "iso:GB"], "spatial_note": "x",
    "derived_from": [{"kind": "dataset", "identifier": "rs1/a/green/2_final/b"}, {"kind": "external", "url": "https://e.org"}],
    "provenance": [{"activity": "clean", "tool": {"name": "x"}, "outputs": ["a.csv"]}],
    "category_history": [{"when": "2026-01-01T00:00:00Z", "by": "k1", "kind": "replaced",
                          "from": ["a"], "to": ["b"]}],
    "vocabulary_version": "2026-10-23",
    "files": [{"path": "a.csv", "checksum_sha256": SHA, "bytes": 3, "format": "text/csv"},
              {"path": "sub/b.csv", "checksum_sha256": SHA, "bytes": 0,
               "temporal": {"start": "2020", "end": "2020"}}],
}
POOL = [None, "", "x", 0, -1, 1.5, True, [], {}, ["x"], [1], [None], {"a": 1}, "red",
        "rs9", "1.0", "v3-0", "2020-13-01", "dataset.x.json", "a\\b", "../a", "a//b",
        ["quant", "quant"], "quant", {"start": "2021", "end": "2020"}, [{"path": "a.csv"}],
        [{"path": "a.csv"}, {"path": "a.csv"}], "licence", ["m49:999"], ["nobody"],
        "not-a-uuid", [{"activity": "nope"}], [{"identifier": "rs2/csac/green/2_final/x"}],
        [{}], ["a", 1], "0_raw", "amber", "centre", sorted(terms)[:3], ["no-such-term"]]


def mutate(doc, rng, depth=0):
    doc = copy.deepcopy(doc)
    for _ in range(rng.randint(1, 3)):
        target = doc
        # walk into a nested container sometimes
        for _ in range(rng.randint(0, 2)):
            kids = [v for v in (target.values() if isinstance(target, dict) else target)
                    if isinstance(v, (dict, list)) and v]
            if not kids:
                break
            target = rng.choice(kids)
        if isinstance(target, dict) and target:
            key = rng.choice(list(target) + ["licence", "extra"])
            if rng.random() < 0.3:
                target.pop(key, None)
            else:
                target[key] = copy.deepcopy(rng.choice(POOL))
        elif isinstance(target, list) and target:
            i = rng.randrange(len(target))
            if rng.random() < 0.3:
                target.pop(i)
            else:
                target[i] = copy.deepcopy(rng.choice(POOL))
    return doc


def outcome(fn, *args):
    try:
        return ("ok", fn(*copy.deepcopy(args)))
    except Exception as e:  # the same exception must come from both
        return ("raised", type(e).__name__, str(e))


rng = random.Random(20261124)
kw = [(None, None, None, None),
      (["quant", "geo"], ["clean", "convert"], ["acled"], {"m49:004", "iso:GB"})]
total_diff = 0
n = diff = nonempty = 0
for _ in range(40000):
    rec = mutate(BASE, rng)
    for extra in kw:
        a = outcome(old_rec.validate_record, rec, terms, *extra)
        b = outcome(new_rec.validate_record, rec, terms, *extra)
        n += 1
        nonempty += a != ("ok", ([], []))
        if a != b:
            diff += 1
            if diff <= 3:
                print("RECORD DIFF", json.dumps(rec)[:300], a, b)
print("validate_record: %d cases, %d with errors, warnings or an exception, %d differ"
      % (n, nonempty, diff))
print("  unmutated record clean:", old_rec.validate_record(BASE, terms)[0] == [],
      new_rec.validate_record(BASE, terms))

total_diff += diff
APOOL = [None, "", "x", 0, [], {}, ["x"], [1], "retired", "current", "2026-01-01",
         "Bad Slug", "forced-labour", ["forced-labour"], ["forced-labour", "forced-labour"],
         "no-such", ["no-such"], {"practices": []}, "practices", "themes"]
POOL[:] = APOOL
n = diff = nonempty = 0
for _ in range(30000):
    doc = mutate(vocab, rng)
    a = outcome(old_auth.validate_authority, doc)
    b = outcome(new_auth.validate_authority, doc)
    n += 1
    nonempty += a != ("ok", [])
    if a != b:
        diff += 1
        if diff <= 3:
            print("AUTHORITY DIFF", a, b)
print("validate_authority: %d cases, %d with errors or an exception, %d differ"
      % (n, nonempty, diff))
print("  bundled vocabulary clean:", old_auth.validate_authority(vocab) == [],
      new_auth.validate_authority(vocab) == [])
sys.exit(1 if total_diff + diff else 0)
