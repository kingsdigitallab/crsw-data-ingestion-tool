"""The changes list only ever grows: every entry on the base branch must
still be there, unchanged and in the same order, on the pull request.

    python scripts/check_append_only.py origin/main vocab.json
"""
import json
import subprocess
import sys


def main(base_ref: str, path: str) -> int:
    try:
        before_text = subprocess.check_output(["git", "show", "%s:%s" % (base_ref, path)],
                                              text=True)
    except subprocess.CalledProcessError:
        print("no %s on %s yet; nothing to compare" % (path, base_ref))
        return 0
    before = json.loads(before_text).get("changes") or []
    after = json.load(open(path, encoding="utf-8")).get("changes") or []
    if after[:len(before)] != before:
        print("the changes list has been edited or reordered; it may only be "
              "appended to", file=sys.stderr)
        return 1
    print("changes: %d before, %d after, history intact" % (len(before), len(after)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
