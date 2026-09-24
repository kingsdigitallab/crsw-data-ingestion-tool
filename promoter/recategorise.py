"""Bring dataset records in place up to the current categories (r9).

Two sources of change, both applied to the record only (never a member,
a label or a key):

- the vocabulary's own changes list (renames, merges, splits and
  retirements), applied mechanically to every record that carries an
  affected term, recorded in the record by the actor "vocabulary";
- a change file: a steward's per-dataset decisions, each naming a
  dataset by identifier or UUID with terms to add, terms to remove, a
  new domain and a reason.

`plan` reads every record and works out what would change; `apply`
writes one planned record the way promotion does (validate, put, read
back, compare). Records that would not change are never written, so a
second run finds nothing to do."""
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from crsw_deposit import deposit_logic, labels as labels_mod, record, vocab

from .checks import _read_json_object
from .log import Log
from .scan import list_datasets

CHANGE_KEYS = ("dataset", "add", "remove", "set_domain", "reason")


class ChangeFileError(ValueError):
    pass


def load_change_file(text: str) -> List[Dict]:
    """The change file as a validated list. Shape:
    [{"dataset": "<identifier or uuid>", "add": [...], "remove": [...],
      "set_domain": "...", "reason": "..."}, ...]"""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ChangeFileError("not JSON: %s" % e)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ChangeFileError("must be a list of changes")
    out = []
    for i, c in enumerate(data):
        label = "change %d" % (i + 1)
        if not isinstance(c, dict):
            raise ChangeFileError("%s is not an object" % label)
        unknown = sorted(k for k in c if k not in CHANGE_KEYS)
        if unknown:
            raise ChangeFileError("%s: unknown key(s) %s; allowed: %s"
                                  % (label, ", ".join(unknown), ", ".join(CHANGE_KEYS)))
        if not isinstance(c.get("dataset"), str) or not c["dataset"].strip():
            raise ChangeFileError("%s: dataset (identifier or uuid) is missing" % label)
        for k in ("add", "remove"):
            v = c.get(k)
            if v is not None and not (isinstance(v, list)
                                      and all(isinstance(s, str) and s for s in v)):
                raise ChangeFileError("%s: %s must be a list of terms" % (label, k))
        if "set_domain" in c and not (isinstance(c["set_domain"], str)
                                      and c["set_domain"]):
            raise ChangeFileError("%s: set_domain must be a domain code" % label)
        if not (c.get("add") or c.get("remove") or c.get("set_domain")):
            raise ChangeFileError("%s: nothing to do (no add, remove or set_domain)"
                                  % label)
        out.append(c)
    return out


@dataclass
class Item:
    prefix: str
    record_key: str
    original: bytes
    before: Dict
    after: Dict
    entries: List[Dict] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.entries)

    def summary(self) -> Dict:
        return {"prefix": self.prefix,
                "subject_before": list(self.before.get("subject") or []),
                "subject_after": list(self.after.get("subject") or []),
                "domain_before": self.before.get("domain"),
                "domain_after": self.after.get("domain"),
                "changes": [{k: v for k, v in e.items() if k != "when"}
                            for e in self.entries]}


@dataclass
class Plan:
    items: List[Item] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)   # not tied to one dataset

    @property
    def to_write(self) -> List[Item]:
        return [i for i in self.items if i.changed and not i.problems]

    @property
    def refused(self) -> List[Item]:
        return [i for i in self.items if i.problems]


def _matches(change: Dict, rec: Dict) -> bool:
    target = change["dataset"].strip()
    return target in (rec.get("identifier"), rec.get("dataset_uuid"))


def plan(client, bucket: str, vocab_doc: Dict, changes: List[Dict],
         who: str, now: str, only: Optional[str] = None) -> Plan:
    """What every record in place would become. `only` limits the walk
    to one dataset (identifier or uuid). Change-file entries that match
    no dataset are problems on the plan."""
    out = Plan()
    terms = vocab.all_terms(vocab_doc)
    codes = vocab.domain_codes(vocab_doc)
    version = vocab_doc.get("vocabulary_version")
    used = [False] * len(changes)
    for ref in list_datasets(client, bucket):
        text, err = _read_json_object(client, bucket, ref.record_key)
        if text is None:
            out.problems.append("%s: could not read record: %s" % (ref.prefix, err))
            continue
        try:
            before, _upgraded = record.parse_record_with_status(text)
        except record.RecordParseError as e:
            out.problems.append("%s: record cannot be used: %s" % (ref.prefix, e))
            continue
        if only and only not in (before.get("identifier"), before.get("dataset_uuid")):
            continue
        item = Item(ref.prefix, ref.record_key, text.encode("utf-8"), before, before)
        if before.get("identifier") != ref.prefix:
            item.problems.append("record's identifier %r does not match its "
                                 "location; moved or hand-edited"
                                 % before.get("identifier"))
            out.items.append(item)
            continue
        after, entries = record.apply_vocabulary_mapping(before, vocab_doc, now)
        for i, c in enumerate(changes):
            if not _matches(c, before):
                continue
            used[i] = True
            try:
                after, more = record.apply_dataset_change(
                    after, c, who, now, terms, codes, vocabulary_version=version)
            except record.RecordChangeError as e:
                item.problems.append("change for %s: %s" % (c["dataset"], e))
                continue
            entries.extend(more)
        item.after, item.entries = after, entries
        if entries:
            errors, _ = record.validate_record(after, terms, codes)
            for e in errors:
                item.problems.append("would leave the record invalid: %s" % e)
        out.items.append(item)
    for c, hit in zip(changes, used):
        if not hit:
            out.problems.append("change for %s: no dataset with that identifier "
                                "or uuid%s" % (c["dataset"],
                                               " under %s" % only if only else ""))
    return out


def apply(client, bucket: str, item: Item, log: Log, who: str) -> bool:
    """Write one planned record in place, exactly as promotion writes a
    record: put, read back, compare. Logs record_rewritten or
    recategorise_failed. Returns True on success."""
    if not item.changed or item.problems:
        raise ValueError("nothing to write for %s" % item.prefix)
    data = deposit_logic.record_bytes(item.after)
    rec = item.after
    try:
        client.put_object(
            Bucket=bucket, Key=item.record_key, Body=data,
            ContentType="application/json",
            Metadata=labels_mod.object_labels(
                rec["dataset_uuid"], hashlib.sha256(data).hexdigest(),
                rec["sensitivity"], who))
        back = client.get_object(Bucket=bucket, Key=item.record_key)["Body"].read()
        if back != data:
            raise RuntimeError("record round-trip mismatch at %s" % item.record_key)
    except Exception as e:
        log.write("recategorise_failed", prefix=item.prefix, error=str(e))
        return False
    log.write("record_rewritten", record=item.record_key, **item.summary())
    return True
