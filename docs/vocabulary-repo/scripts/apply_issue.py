"""Turn a submitted issue form into a crsw-vocab command and run it.

Reads ISSUE_BODY (the form as GitHub renders it: "### Field" headings
followed by the value), ISSUE_LABELS (change:<kind>), ISSUE_AUTHOR and
ISSUE_NUMBER from the environment. Writes vocab.json in the current
directory through crsw-vocab, which validates before writing. Prints
`title=...` for the workflow to use on the commit and pull request.
Exits non-zero, with the reason on stderr, if the form cannot be
applied; the workflow then comments on the issue."""
import os
import sys

from crsw_deposit import vocab_cli

NO_RESPONSE = "_No response_"


def fields(body: str) -> dict:
    """{heading: value} from an issue-form body."""
    out = {}
    heading = None
    lines = []
    for line in (body or "").splitlines():
        if line.startswith("### "):
            if heading is not None:
                out[heading] = "\n".join(lines).strip()
            heading = line[4:].strip()
            lines = []
        else:
            lines.append(line)
    if heading is not None:
        out[heading] = "\n".join(lines).strip()
    return {k: ("" if v == NO_RESPONSE else v) for k, v in out.items()}


def value(form: dict, heading: str, required: bool = True) -> str:
    v = form.get(heading, "").strip()
    if required and not v:
        raise SystemExit("the form field %r is empty" % heading)
    return v


def slug_lines(text: str):
    return [l.strip() for l in text.splitlines() if l.strip()]


def main() -> int:
    form = fields(os.environ.get("ISSUE_BODY", ""))
    labels = [l.strip() for l in os.environ.get("ISSUE_LABELS", "").split(",")]
    kinds = [l.split(":", 1)[1] for l in labels if l.startswith("change:")]
    if len(kinds) != 1:
        raise SystemExit("expected exactly one change:<kind> label, got %s" % labels)
    kind = kinds[0]
    author = os.environ.get("ISSUE_AUTHOR") or "unknown"
    number = os.environ.get("ISSUE_NUMBER") or "?"
    note = "issue #%s: %s" % (number, value(form, "Reason").splitlines()[0][:120])
    common = ["--by", author, "--note", note]
    date = form.get("Effective date (optional)", "").strip()
    if date:
        common += ["--date", date]

    if kind == "add":
        argv = ["add", value(form, "Slug"), "--facet", value(form, "Facet"),
                "--label", value(form, "Label")]
        broader = value(form, "Broader term (optional)", required=False)
        if broader:
            argv += ["--broader", broader]
        title = "add %s" % value(form, "Slug")
    elif kind == "rename":
        argv = ["rename", value(form, "Old term"), value(form, "New term")]
        label = value(form, "New label (optional)", required=False)
        if label:
            argv += ["--label", label]
        title = "rename %s to %s" % (value(form, "Old term"), value(form, "New term"))
    elif kind == "merge":
        old = slug_lines(value(form, "Terms to merge (one per line)"))
        target = value(form, "Target term")
        target_label = value(form, "Target label (only if the target is new)",
                             required=False)
        into = "%s=%s" % (target, target_label) if target_label else target
        argv = ["merge"] + old + ["--into", into]
        title = "merge %s into %s" % (", ".join(old), target)
    elif kind == "split":
        old = value(form, "Term to split")
        new = []
        for line in slug_lines(value(form, "New terms (one per line, as slug = Label)")):
            if "=" in line:
                slug, label = line.split("=", 1)
                new.append("%s=%s" % (slug.strip(), label.strip()))
            else:
                new.append(line)
        argv = ["split", old, "--into"] + new
        title = "split %s into %s" % (old, ", ".join(n.split("=")[0] for n in new))
    elif kind == "retire":
        argv = ["retire", value(form, "Term to retire")]
        title = "retire %s" % value(form, "Term to retire")
    elif kind == "move":
        term = value(form, "Term to move")
        broader = value(form, "New broader term")
        argv = ["move", term] + (["--top"] if broader.lower() == "top"
                                 else ["--under", broader])
        title = "move %s %s" % (term, "to the top level" if broader.lower() == "top"
                                else "under %s" % broader)
    else:
        raise SystemExit("unknown change kind %r" % kind)

    code = vocab_cli.main(["--file", "vocab.json"] + argv + common)
    if code != 0:
        return code
    print("title=vocabulary: %s" % title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
