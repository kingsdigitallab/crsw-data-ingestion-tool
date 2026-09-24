"""crsw-vocab: edit the term authority file without typing JSON (r9 §1.6).

    crsw-vocab validate [--file vocab.json]
    crsw-vocab tree FACET
    crsw-vocab add SLUG --facet F --label "Text" [--broader PARENT]
    crsw-vocab rename OLD NEW [--label "Text"]
    crsw-vocab merge OLD [OLD ...] --into TARGET[=Label]
    crsw-vocab split OLD --into NEW=Label NEW2=Label [...]
    crsw-vocab retire SLUG
    crsw-vocab move SLUG --under PARENT | --top

Every editing command takes --by (default: your login), --date (default:
today), --note. The file is validated after the change and only written
if it passes; the input file is never touched on refusal. No git calls:
commit the result yourself, or let the vocabulary repo's Action do it.
Stdlib only, the only user I/O in the package besides deposit.py.
"""
import argparse
import datetime
import getpass
import json
import sys
from pathlib import Path

from . import authority

DEFAULT_FILE = "vocab.json"


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SystemExit("Could not read %s: %s" % (path, e))
    except ValueError as e:
        raise SystemExit("%s is not JSON: %s" % (path, e))
    if not authority.is_authority(doc):
        raise SystemExit("%s is not an authority file (no terms list); see "
                         "docs/specs/DEPOSIT_TOOL_SPEC_R9.md" % path)
    return doc


def _write(path: Path, doc: dict) -> None:
    path.write_text(authority.dump_authority(doc), encoding="utf-8")


def _slug_label(text: str):
    """NEW=Label -> (slug, label); a bare slug -> (slug, None)."""
    if "=" in text:
        slug, label = text.split("=", 1)
        return slug.strip(), label.strip() or None
    return text.strip(), None


def _base(args) -> dict:
    change = {"date": args.date, "by": args.by}
    if args.note:
        change["note"] = args.note
    return change


def _apply(args, doc: dict, changes) -> dict:
    """Apply changes in order; any refusal aborts before writing."""
    for change in changes:
        try:
            doc = authority.apply_change(doc, change)
        except authority.AuthorityError as e:
            raise SystemExit("Refused: %s" % e)
    return doc


def cmd_validate(args) -> int:
    doc = _load(args.file)
    errors = authority.validate_authority(doc)
    if errors:
        for e in errors:
            print("- " + e)
        print("%s: %d problem(s)" % (args.file, len(errors)))
        return 1
    n = len(authority.current_terms(doc))
    print("%s: valid, version %s, %d current term(s), %d change(s)"
          % (args.file, doc.get("vocabulary_version"), n,
             len(doc.get("changes") or [])))
    return 0


def cmd_tree(args) -> int:
    doc = _load(args.file)
    facets = authority.facets_of(doc)
    names = [args.facet] if args.facet else list(facets)
    for name in names:
        if name not in facets:
            raise SystemExit("no facet called %r; facets are: %s"
                             % (name, ", ".join(facets)))
        print(name)
        by_slug = authority.terms_by_slug(doc)
        for slug, depth in authority.tree(doc, name):
            print("  " * (depth + 1) + "%s  %s" % (slug, by_slug[slug].get("label", "")))
    return 0


def cmd_add(args) -> int:
    doc = _load(args.file)
    change = _base(args)
    change.update(kind="add", to=[args.slug], facet=args.facet, label=args.label)
    if args.broader:
        change["broader"] = args.broader
    _write(args.file, _apply(args, doc, [change]))
    print("added %s to %s" % (args.slug, args.facet))
    return 0


def cmd_rename(args) -> int:
    doc = _load(args.file)
    change = _base(args)
    change.update({"kind": "rename", "from": [args.old], "to": [args.new]})
    if args.label:
        change["label"] = args.label
    _write(args.file, _apply(args, doc, [change]))
    print("renamed %s to %s" % (args.old, args.new))
    return 0


def _ensure_terms(args, doc, specs, facet):
    """add changes for any NEW=Label that does not exist yet; a bare
    slug must exist already."""
    by_slug = authority.terms_by_slug(doc)
    changes = []
    for slug, label in specs:
        if slug in by_slug:
            continue
        if not label:
            raise SystemExit("Refused: %s does not exist; give it a label "
                             "as %s=Label to create it" % (slug, slug))
        c = _base(args)
        c.update(kind="add", to=[slug], facet=facet, label=label)
        changes.append(c)
    return changes


def cmd_merge(args) -> int:
    doc = _load(args.file)
    by_slug = authority.terms_by_slug(doc)
    for old in args.old:
        if old not in by_slug:
            raise SystemExit("Refused: unknown term %r" % old)
    facet = by_slug[args.old[0]]["facet"]
    target = _slug_label(args.into)
    changes = _ensure_terms(args, doc, [target], facet)
    c = _base(args)
    c.update({"kind": "merge", "from": list(args.old), "to": [target[0]]})
    changes.append(c)
    _write(args.file, _apply(args, doc, changes))
    print("merged %s into %s" % (", ".join(args.old), target[0]))
    return 0


def cmd_split(args) -> int:
    doc = _load(args.file)
    by_slug = authority.terms_by_slug(doc)
    if args.old not in by_slug:
        raise SystemExit("Refused: unknown term %r" % args.old)
    specs = [_slug_label(t) for t in args.into]
    changes = _ensure_terms(args, doc, specs, by_slug[args.old]["facet"])
    c = _base(args)
    c.update({"kind": "split", "from": [args.old], "to": [s for s, _ in specs]})
    changes.append(c)
    _write(args.file, _apply(args, doc, changes))
    print("split %s into %s" % (args.old, ", ".join(s for s, _ in specs)))
    return 0


def cmd_retire(args) -> int:
    doc = _load(args.file)
    c = _base(args)
    c.update({"kind": "retire", "from": [args.slug]})
    _write(args.file, _apply(args, doc, [c]))
    print("retired %s" % args.slug)
    return 0


def cmd_move(args) -> int:
    doc = _load(args.file)
    c = _base(args)
    c.update({"kind": "move", "from": [args.slug],
              "broader": None if args.top else args.under})
    _write(args.file, _apply(args, doc, [c]))
    print("moved %s %s" % (args.slug, "to the top level" if args.top
                           else "under %s" % args.under))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crsw-vocab",
        description="Edit the CRSW term authority file; every change is "
                    "validated and recorded in the file's change list.")
    p.add_argument("--file", type=Path, default=Path(DEFAULT_FILE),
                   help="the authority file (default: ./%s)" % DEFAULT_FILE)
    sub = p.add_subparsers(dest="command", required=True)

    try:
        login = getpass.getuser()
    except Exception:
        login = "unknown"

    def editing(name, help_):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--by", default=login,
                       help="who decided (default: your login)")
        s.add_argument("--date", default=datetime.date.today().isoformat(),
                       help="effective date, YYYY-MM-DD (default: today)")
        s.add_argument("--note", default=None,
                       help="why, or the issue number")
        return s

    s = sub.add_parser("validate", help="check the file against every rule")
    s.set_defaults(func=cmd_validate)
    s = sub.add_parser("tree", help="print a facet's terms, indented")
    s.add_argument("facet", nargs="?", default=None)
    s.set_defaults(func=cmd_tree)

    s = editing("add", "add a term")
    s.add_argument("slug")
    s.add_argument("--facet", required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--broader", default=None, help="parent term, same facet")
    s.set_defaults(func=cmd_add)

    s = editing("rename", "give a term a new slug; the old one is retired")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--label", default=None, help="new label (default: keep)")
    s.set_defaults(func=cmd_rename)

    s = editing("merge", "retire terms in favour of one target")
    s.add_argument("old", nargs="+")
    s.add_argument("--into", required=True,
                   help="TARGET, or TARGET=Label to create it")
    s.set_defaults(func=cmd_merge)

    s = editing("split", "retire a term in favour of two or more")
    s.add_argument("old")
    s.add_argument("--into", nargs="+", required=True,
                   help="NEW=Label ... (an existing slug needs no label)")
    s.set_defaults(func=cmd_split)

    s = editing("retire", "retire a term with no successor")
    s.add_argument("slug")
    s.set_defaults(func=cmd_retire)

    s = editing("move", "move a term under another, or to the top level")
    s.add_argument("slug")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--under", default=None, help="new parent, same facet")
    g.add_argument("--top", action="store_true", help="no parent")
    s.set_defaults(func=cmd_move)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
