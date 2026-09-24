"""Term authority file: the vocabulary with its history (r9).

The vocabulary file lists every subject term ever approved, current or
retired, and an append-only list of the changes that got it there. This
module validates such a file, derives the flat facet lists older readers
expect, applies a change to it, and maps a record's subjects forward
through the changes. Pure functions, stdlib only, no I/O: the vocabulary
repo's CI, the command line, the promoter and the deposit tool all call
the same code.

A file with no `terms` key is the pre-r9 flat shape and is left alone by
everything here except `facets_of`, which reads it as-is. A file whose
terms have no `broader` parents is flat too; every function below then
behaves exactly as if the hierarchy did not exist.
"""
import copy
import datetime
import json
import re
from typing import Dict, List, Optional, Tuple

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATUSES = ("current", "retired")
CHANGE_KINDS = ("add", "rename", "merge", "split", "retire", "move")

# What a mapping reports for each thing it did to a subject list.
MAPPED_REPLACED = "replaced"          # rename or merge: one term for others
MAPPED_SPLIT_REVIEW = "split_review"  # split: all successors, needs a person
MAPPED_REMOVED = "removed"            # retirement with no successor


class AuthorityError(ValueError):
    """A change that the authority file cannot accept."""


# --- reading -----------------------------------------------------------

def is_authority(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("terms"), list)


def terms_by_slug(doc: dict) -> Dict[str, dict]:
    return {t["slug"]: t for t in doc.get("terms") or []
            if isinstance(t, dict) and isinstance(t.get("slug"), str)}


def current_terms(doc: dict) -> List[dict]:
    return [t for t in doc.get("terms") or []
            if isinstance(t, dict) and t.get("status") == "current"]


def facet_order(doc: dict) -> List[str]:
    """Facet names in file order: the `facets` block's order when there
    is one, else first appearance in `terms`."""
    order = []
    if isinstance(doc.get("facets"), dict):
        order.extend(doc["facets"].keys())
    for t in doc.get("terms") or []:
        if isinstance(t, dict) and t.get("facet") and t["facet"] not in order:
            order.append(t["facet"])
    return order


def facets_from_terms(doc: dict) -> Dict[str, List[str]]:
    """The flat facet lists (facet -> current slugs, file order) that
    `vocab.all_terms` and the subject listing read."""
    facets = {name: [] for name in facet_order(doc)}
    for t in current_terms(doc):
        facets.setdefault(t["facet"], []).append(t["slug"])
    return facets


def facets_of(doc: dict) -> Dict[str, List[str]]:
    """Facet lists for either shape of file."""
    if is_authority(doc):
        return facets_from_terms(doc)
    return {k: list(v) for k, v in (doc.get("facets") or {}).items()}


def normalise(doc: dict) -> dict:
    """An authority file with its derived `facets` filled in, so readers
    that only know the flat shape keep working. Returns a copy."""
    if not is_authority(doc):
        return doc
    out = dict(doc)
    out["facets"] = facets_from_terms(doc)
    return out


# --- hierarchy ---------------------------------------------------------

def children_of(doc: dict, slug: str) -> List[str]:
    return [t["slug"] for t in current_terms(doc) if t.get("broader") == slug]


def narrower(doc: dict, slug: str) -> List[str]:
    """Every current term below `slug`, depth first, file order."""
    out = []
    for child in children_of(doc, slug):
        out.append(child)
        out.extend(narrower(doc, child))
    return out


def tree(doc: dict, facet: str) -> List[Tuple[str, int]]:
    """Current terms of a facet as (slug, depth), roots in file order
    and each root followed by its descendants. Depth is 0 everywhere
    for a flat vocabulary."""
    out = []

    def walk(parent: Optional[str], depth: int):
        for t in current_terms(doc):
            if t.get("facet") == facet and t.get("broader") == parent:
                out.append((t["slug"], depth))
                walk(t["slug"], depth + 1)

    walk(None, 0)
    return out


def _ancestors(by_slug: Dict[str, dict], slug: str) -> List[str]:
    seen = []
    while slug in by_slug and by_slug[slug].get("broader"):
        slug = by_slug[slug]["broader"]
        if slug in seen:
            return seen + [slug]
        seen.append(slug)
    return seen


# --- validation --------------------------------------------------------

def validate_authority(doc) -> List[str]:
    """Every rule the file must satisfy, as plain messages. Empty means
    the file is good. Runs in the vocabulary repo's CI and in the tool
    before a fetched file is trusted."""
    errors = []
    if not isinstance(doc, dict):
        return ["the vocabulary is not a JSON object"]
    if not isinstance(doc.get("vocabulary_version"), str) or not DATE_RE.match(
            doc["vocabulary_version"]):
        errors.append("vocabulary_version must be a date like 2026-07-23")
    terms = doc.get("terms")
    if not isinstance(terms, list) or not terms:
        return errors + ["terms must be a non-empty list"]

    by_slug = {}
    for i, t in enumerate(terms):
        label = "terms[%d]" % (i + 1)
        if not isinstance(t, dict):
            errors.append("%s is not an object" % label)
            continue
        slug = t.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            errors.append("%s: slug %r is not lower-case words joined by "
                          "hyphens" % (label, slug))
            continue
        label = "term %s" % slug
        if slug in by_slug:
            errors.append("%s appears twice" % label)
            continue
        by_slug[slug] = t
        if not isinstance(t.get("facet"), str) or not t["facet"]:
            errors.append("%s: facet is missing" % label)
        if not isinstance(t.get("label"), str) or not t["label"].strip():
            errors.append("%s: label is missing" % label)
        if t.get("status") not in STATUSES:
            errors.append("%s: status must be current or retired" % label)
        for field in ("since", "until"):
            value = t.get(field)
            if value is not None and not (isinstance(value, str)
                                          and DATE_RE.match(value)):
                errors.append("%s: %s %r is not a date" % (label, field, value))
        if t.get("status") == "retired":
            if not t.get("until"):
                errors.append("%s: a retired term needs an until date" % label)
            rb = t.get("replaced_by")
            if rb is not None and not (isinstance(rb, list)
                                       and all(isinstance(s, str) for s in rb)):
                errors.append("%s: replaced_by must be a list of slugs" % label)
        elif t.get("until") or t.get("replaced_by"):
            errors.append("%s: only a retired term has until or replaced_by"
                          % label)
        broader = t.get("broader")
        if broader is not None and not isinstance(broader, str):
            errors.append("%s: broader must be a slug or null" % label)

    # Cross-term rules.
    for slug, t in by_slug.items():
        for succ in t.get("replaced_by") or []:
            if succ not in by_slug:
                errors.append("term %s: replaced_by names unknown term %r"
                              % (slug, succ))
            elif succ == slug:
                errors.append("term %s: replaced by itself" % slug)
        broader = t.get("broader")
        if isinstance(broader, str):
            parent = by_slug.get(broader)
            if parent is None:
                errors.append("term %s: broader names unknown term %r"
                              % (slug, broader))
            else:
                if parent.get("facet") != t.get("facet"):
                    errors.append("term %s: broader %s is in another facet"
                                  % (slug, broader))
                if t.get("status") == "current" and parent.get("status") != "current":
                    errors.append("term %s: broader %s is retired"
                                  % (slug, broader))
                chain = _ancestors(by_slug, slug)
                if slug in chain:
                    errors.append("term %s: broader chain loops" % slug)

    # The flat lists, when present, must be exactly the derivation.
    if "facets" in doc:
        derived = facets_from_terms(doc)
        if not isinstance(doc["facets"], dict):
            errors.append("facets must be an object")
        else:
            for name, listed in doc["facets"].items():
                if list(listed) != derived.get(name, []):
                    errors.append("facets.%s does not match the current "
                                  "terms; regenerate it" % name)
            for name in derived:
                if name not in doc["facets"]:
                    errors.append("facets is missing %s" % name)

    errors.extend(_validate_changes(doc, by_slug))
    return errors


def _validate_changes(doc: dict, by_slug: Dict[str, dict]) -> List[str]:
    errors = []
    changes = doc.get("changes")
    if changes is None:
        return errors
    if not isinstance(changes, list):
        return ["changes must be a list"]
    last = ""
    for i, c in enumerate(changes):
        label = "changes[%d]" % (i + 1)
        if not isinstance(c, dict):
            errors.append("%s is not an object" % label)
            continue
        date = c.get("date")
        if not isinstance(date, str) or not DATE_RE.match(date):
            errors.append("%s: date %r is not a date" % (label, date))
        elif date < last:
            errors.append("%s: dated %s, before the change above it (%s); "
                          "changes are appended in order" % (label, date, last))
        else:
            last = date
        kind = c.get("kind")
        if kind not in CHANGE_KINDS:
            errors.append("%s: kind %r is not one of %s"
                          % (label, kind, "/".join(CHANGE_KINDS)))
            continue
        if not isinstance(c.get("by"), str) or not c["by"].strip():
            errors.append("%s: by (who decided) is missing" % label)
        for field in ("from", "to"):
            value = c.get(field)
            if value is not None and not (isinstance(value, list)
                                          and all(isinstance(s, str) for s in value)):
                errors.append("%s: %s must be a list of slugs" % (label, field))
        frm = c.get("from") or []
        to = c.get("to") or []
        for slug in list(frm) + list(to):
            if isinstance(slug, str) and slug not in by_slug:
                errors.append("%s: names unknown term %r" % (label, slug))
        if kind == "add" and len(to) != 1:
            errors.append("%s: add names exactly one term in to" % label)
        if kind == "rename" and (len(frm) != 1 or len(to) != 1):
            errors.append("%s: rename takes one term in from and one in to"
                          % label)
        if kind == "merge" and (len(frm) < 1 or len(to) != 1):
            errors.append("%s: merge takes one or more terms in from and one "
                          "in to" % label)
        if kind == "split" and (len(frm) != 1 or len(to) < 2):
            errors.append("%s: split takes one term in from and two or more "
                          "in to" % label)
        if kind == "retire" and (len(frm) != 1 or to):
            errors.append("%s: retire takes one term in from and nothing "
                          "in to" % label)
        if kind == "move":
            if len(frm) != 1:
                errors.append("%s: move takes one term in from" % label)
            if "broader" not in c:
                errors.append("%s: move needs broader (a slug or null)" % label)
    return errors


# --- applying a change -------------------------------------------------

def _require(cond: bool, message: str) -> None:
    if not cond:
        raise AuthorityError(message)


def apply_change(doc: dict, change: dict) -> dict:
    """The authority file after one change, as a new document. The
    change is appended to `changes`, the terms it names are updated,
    `facets` is regenerated and `vocabulary_version` moves to the
    change's date if that is later. Anything the validator would refuse
    raises AuthorityError and the input is untouched."""
    _require(is_authority(doc), "not an authority file (no terms list)")
    out = copy.deepcopy(doc)
    change = dict(change)
    kind = change.get("kind")
    _require(kind in CHANGE_KINDS,
             "kind %r is not one of %s" % (kind, "/".join(CHANGE_KINDS)))
    date = change.get("date") or datetime.date.today().isoformat()
    _require(bool(DATE_RE.match(str(date))), "date %r is not a date" % date)
    change["date"] = date
    _require(isinstance(change.get("by"), str) and change["by"].strip(),
             "by (who decided) is missing")
    by_slug = terms_by_slug(out)
    frm = list(change.get("from") or [])
    to = list(change.get("to") or [])

    def current(slug):
        _require(slug in by_slug, "unknown term %r" % slug)
        _require(by_slug[slug].get("status") == "current",
                 "term %s is already retired" % slug)
        return by_slug[slug]

    def retire(slug, successors):
        t = current(slug)
        _require(not children_of(out, slug),
                 "term %s has narrower terms; move them first" % slug)
        t["status"] = "retired"
        t["until"] = date
        if successors:
            t["replaced_by"] = list(successors)

    def new_term(slug, facet, label, broader):
        _require(bool(SLUG_RE.match(slug or "")),
                 "slug %r is not lower-case words joined by hyphens" % slug)
        _require(slug not in by_slug, "term %s already exists" % slug)
        _require(isinstance(facet, str) and facet, "facet is missing")
        _require(isinstance(label, str) and label.strip(), "label is missing")
        if broader:
            parent = current(broader)
            _require(parent.get("facet") == facet,
                     "broader %s is in facet %s, not %s"
                     % (broader, parent.get("facet"), facet))
        t = {"slug": slug, "facet": facet, "label": label.strip(),
             "status": "current", "since": date}
        if broader:
            t["broader"] = broader
        out["terms"].append(t)
        by_slug[slug] = t
        return t

    if kind == "add":
        _require(len(to) == 1, "add names exactly one term in to")
        new_term(to[0], change.get("facet"), change.get("label"),
                 change.get("broader"))
    elif kind == "rename":
        _require(len(frm) == 1 and len(to) == 1,
                 "rename takes one term in from and one in to")
        old = current(frm[0])
        new = new_term(to[0], old["facet"],
                       change.get("label") or old["label"], old.get("broader"))
        for child in children_of(out, frm[0]):
            by_slug[child]["broader"] = new["slug"]
        retire(frm[0], [new["slug"]])
    elif kind == "merge":
        _require(len(frm) >= 1 and len(to) == 1,
                 "merge takes one or more terms in from and one in to")
        target = current(to[0])
        for slug in frm:
            _require(slug != to[0], "cannot merge %s into itself" % slug)
            _require(current(slug)["facet"] == target["facet"],
                     "term %s is in another facet than %s" % (slug, to[0]))
            retire(slug, [to[0]])
    elif kind == "split":
        _require(len(frm) == 1 and len(to) >= 2,
                 "split takes one term in from and two or more in to")
        old = current(frm[0])
        for slug in to:
            _require(slug != frm[0], "cannot split %s into itself" % slug)
            _require(current(slug)["facet"] == old["facet"],
                     "term %s is in another facet than %s" % (slug, frm[0]))
        retire(frm[0], to)
    elif kind == "retire":
        _require(len(frm) == 1 and not to,
                 "retire takes one term in from and nothing in to")
        retire(frm[0], [])
    elif kind == "move":
        _require(len(frm) == 1, "move takes one term in from")
        _require("broader" in change, "move needs broader (a slug or null)")
        t = current(frm[0])
        broader = change["broader"]
        if broader is not None:
            parent = current(broader)
            _require(parent["facet"] == t["facet"],
                     "broader %s is in another facet" % broader)
            _require(broader != frm[0] and broader not in narrower(out, frm[0]),
                     "moving %s under %s would make a loop" % (frm[0], broader))
            t["broader"] = broader
        else:
            t.pop("broader", None)

    out.setdefault("changes", []).append(change)
    out["facets"] = facets_from_terms(out)
    if date > str(out.get("vocabulary_version") or ""):
        out["vocabulary_version"] = date
    errors = validate_authority(out)
    _require(not errors, "; ".join(errors))
    return out


# --- mapping subjects forward ------------------------------------------

def map_subjects(subjects: List[str], doc: dict) -> Tuple[List[str], List[dict]]:
    """A record's subject list brought up to the current vocabulary, and
    what was done to it. Renames and merges substitute; a split gives
    every successor and reports `split_review`; a retirement with no
    successor drops the term and reports `removed`. Order is kept,
    duplicates collapse, and a list that is already current comes back
    unchanged with nothing reported, so mapping twice is a no-op.

    Changes are replayed in date order. A retired term with no change
    entry (a hand-maintained file) is still handled from its own
    `replaced_by`, so the result never carries a retired term."""
    if not is_authority(doc):
        return list(subjects), []
    by_slug = terms_by_slug(doc)
    current = list(subjects)
    applied = []

    def substitute(old_terms, new_terms, kind, date):
        nonlocal current
        hit = [s for s in old_terms if s in current]
        if not hit:
            return
        result = []
        placed = False
        for s in current:
            if s in hit:
                if not placed:
                    result.extend(n for n in new_terms if n not in result)
                    placed = True
            elif s not in result:
                result.append(s)
        if not new_terms:
            result = [s for s in current if s not in hit]
        current = result
        applied.append({"kind": kind, "from": hit, "to": list(new_terms),
                        "date": date})

    for c in doc.get("changes") or []:
        kind = c.get("kind")
        frm = c.get("from") or []
        to = c.get("to") or []
        if kind in ("rename", "merge"):
            substitute(frm, to, MAPPED_REPLACED, c.get("date"))
        elif kind == "split":
            substitute(frm, to, MAPPED_SPLIT_REVIEW, c.get("date"))
        elif kind == "retire":
            substitute(frm, [], MAPPED_REMOVED, c.get("date"))

    # Safety net for retired terms the changes list did not cover.
    for slug in list(current):
        t = by_slug.get(slug)
        if t and t.get("status") == "retired":
            successors = t.get("replaced_by") or []
            kind = (MAPPED_SPLIT_REVIEW if len(successors) > 1
                    else MAPPED_REPLACED if successors else MAPPED_REMOVED)
            substitute([slug], successors, kind, t.get("until"))
    return current, applied


# --- writing -----------------------------------------------------------

def dump_authority(doc: dict) -> str:
    """The file as text, one term, one change, one domain per line, so
    a change shows as a small diff in review. `facets` is regenerated."""
    doc = normalise(doc)

    def block(key, items):
        items = list(items)
        if not items:
            return ['  "%s": [],' % key]
        return ['  "%s": [' % key] + [
            "    %s%s" % (json.dumps(item, ensure_ascii=False),
                          "," if i < len(items) - 1 else "")
            for i, item in enumerate(items)] + ["  ],"]

    lines = ["{", '  "vocabulary_version": %s,' % json.dumps(doc["vocabulary_version"])]
    for key in ("domains", "activities"):
        if key in doc:
            lines += block(key, doc[key])
    lines += block("terms", doc["terms"])
    lines += block("changes", doc.get("changes") or [])
    facets = list(doc["facets"].items())
    lines += ['  "facets": {'] + [
        '    %s: %s%s' % (json.dumps(name), json.dumps(slugs),
                          "," if i < len(facets) - 1 else "")
        for i, (name, slugs) in enumerate(facets)] + ["  }", "}", ""]
    return "\n".join(lines)
