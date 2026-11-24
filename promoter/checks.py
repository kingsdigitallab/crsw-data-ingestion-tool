"""Every check a deposit must pass before it leaves staging. Each failure
is a named problem; the deposit is left where it is and reported.

Nothing here moves or deletes anything."""
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from crsw_deposit import (deposit_logic, filetypes, keys, labels as labels_mod,
                          record, vocab)
from crsw_web.deposits import STATUS_COMPLETE, Deposit, DepositStore

from .config import PromoterConfig
from .resolve import lookup, read_json_object

CHUNK = 8 * 1024 * 1024


@dataclass
class Report:
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    existing_record: Optional[Dict] = None
    staged_record: Optional[Dict] = None
    total_bytes: int = 0
    # r9: set when the staged record's subject terms were stale and mapped
    # cleanly (rename/merge/retire) to the current vocabulary.
    mapped_subjects: Optional[List[str]] = None
    mapping_entries: List[Dict] = field(default_factory=list)
    mapped_vocabulary_version: Optional[str] = None
    # r8 §4: derived_from with parents' uuid/version filled in from the
    # store, when any reference needed it; None when nothing changed.
    resolved_derived_from: Optional[List[Dict]] = None
    resolved_references: List[Dict] = field(default_factory=list)
    unresolved_references: List[Dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


_read_json_object = read_json_object


def _sha256_and_head(client, bucket, key, n: int) -> Tuple[str, bytes]:
    """The object's SHA-256 and its first `n` bytes, from one read."""
    h = hashlib.sha256()
    head = b""
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in body.iter_chunks(CHUNK):
        h.update(chunk)
        if len(head) < n:
            head += chunk[:n - len(head)]
    return h.hexdigest(), head


def _head_of_object(client, bucket, key, n: int) -> bytes:
    """The object's first `n` bytes alone (when checksums are not being
    verified, so nothing else reads it). Not for an empty object."""
    body = client.get_object(Bucket=bucket, Key=key,
                             Range="bytes=0-%d" % (n - 1))["Body"]
    return body.read()


def _check_control(dep: Deposit, rep: Report) -> None:
    """Report a deposit that is not complete, has no record key or has
    no files."""
    p = rep.problems
    if dep.status != STATUS_COMPLETE:
        p.append("deposit status is %r, not complete" % dep.status)
    if not dep.record_key:
        p.append("deposit has no record_key")
    if not dep.entries:
        p.append("deposit has no files")


def _read_staged_record(dep: Deposit, client, bucket, staged_prefix: str,
                        rep: Report) -> Optional[Dict]:
    """Read and parse the staged record. Return it, or None with the
    reason reported."""
    p = rep.problems
    expected_record_key = staged_prefix + "/" + keys.record_filename(dep.meta["dataset"])
    if dep.record_key != expected_record_key:
        p.append("record_key %r is not where the layout says (%r)"
                 % (dep.record_key, expected_record_key))
    text, err = _read_json_object(client, bucket, dep.record_key)
    staged_rec = None
    if text is None:
        p.append("staged record %s: %s" % (dep.record_key, err))
    else:
        try:
            staged_rec, upgraded = record.parse_record_with_status(text)
        except record.RecordParseError as e:
            p.append("staged record cannot be used: %s" % e)
        else:
            if upgraded:
                rep.warnings.append("staged record was schema 0.5; it will be "
                                    "written as %s" % record.SCHEMA_VERSION)
    return staged_rec


def _map_stale_subjects(staged_rec: Optional[Dict], vocab_doc: Optional[Dict],
                        rep: Report) -> Optional[Dict]:
    """Bring the staged record's subject terms up to the vocabulary and
    return the record to use. A split is reported as a problem."""
    p = rep.problems
    if staged_rec and vocab_doc:
        mapped, entries = record.apply_vocabulary_mapping(
            staged_rec, vocab_doc, record.utc_now_iso())
        splits = [e for e in entries if e["kind"] == "split_review"]
        if splits:
            for e in splits:
                p.append("subject %s was split into %s; a person must choose "
                         "(re-finalise the deposit with the right term(s))"
                         % (", ".join(e["from"]), ", ".join(e["to"])))
        elif entries:
            rep.warnings.append("stale subject terms mapped to the current "
                                "vocabulary: %s" % "; ".join(
                                    "%s -> %s" % (", ".join(e.get("from") or []),
                                                  ", ".join(e.get("to") or []) or "(removed)")
                                    for e in entries))
            rep.mapped_subjects = list(mapped["subject"])
            rep.mapping_entries = entries
            rep.mapped_vocabulary_version = mapped.get("vocabulary_version")
            staged_rec = mapped
    return staged_rec


def _resolve_derived_from(staged_rec: Optional[Dict], client, bucket,
                          rep: Report) -> None:
    """Look up the datasets the staged record says it was derived from,
    and note on the report which were found."""
    if staged_rec and isinstance(staged_rec.get("derived_from"), list):
        # r8 §4: resolve, don't invent. A parent that is not there is a
        # warning; the reference is left as typed and the deposit goes on.
        refs, resolved, unresolved = lookup(client, bucket, staged_rec["derived_from"])
        rep.resolved_references = resolved
        rep.unresolved_references = unresolved
        if resolved:
            rep.resolved_derived_from = refs
        for u in unresolved:
            rep.warnings.append(
                "derived_from[%d] %s: %s in the store; reference left as typed"
                % (u["index"], u["identifier"],
                   "no dataset record" if u["reason"] == "absent" else u["reason"]))


def _check_staged_record(staged_rec: Optional[Dict], dep: Deposit,
                         vocab_terms: Set[str], domain_codes: List[str],
                         vocab_doc: Optional[Dict],
                         provider_slugs: Optional[List[str]],
                         place_codes: Optional[Set[str]], rep: Report) -> None:
    """Report a staged record that is not valid, or that disagrees with
    the deposit about its identifier, uuid or files."""
    p = rep.problems
    if staged_rec:
        errors, _ = record.validate_record(
            staged_rec, vocab_terms, domain_codes,
            vocab.activity_codes(vocab_doc) if vocab_doc else None,
            provider_slugs=provider_slugs, place_codes=place_codes)
        for e in errors:
            p.append("staged record invalid: %s" % e)
        if staged_rec.get("identifier") != dep.prefix:
            p.append("staged record identifier %r does not match the deposit's "
                     "prefix %r" % (staged_rec.get("identifier"), dep.prefix))
        if staged_rec.get("dataset_uuid") != dep.dataset_uuid:
            p.append("staged record dataset_uuid does not match the deposit's")
        want = sorted((e["path"], e["checksum_sha256"], e["bytes"]) for e in dep.entries)
        have = sorted((e.get("path"), e.get("checksum_sha256"), e.get("bytes"))
                      for e in staged_rec.get("files", []))
        if want != have:
            p.append("staged record's files manifest differs from the deposit's entries")


def _check_members(dep: Deposit, client, bucket, cfg: PromoterConfig,
                   staged_prefix: str, verify_checksums: bool,
                   rep: Report) -> int:
    """Check each member's size limit, labels, checksum and opening
    bytes. Return the total bytes of the deposit."""
    p = rep.problems
    total = 0
    for entry in dep.entries:
        key = staged_prefix + "/" + entry["path"]
        total += entry["bytes"]
        if cfg.max_object_bytes is not None and entry["bytes"] > cfg.max_object_bytes:
            p.append("%s is %d bytes, over the %d-byte object limit"
                     % (entry["path"], entry["bytes"], cfg.max_object_bytes))
        head = client.head_object(Bucket=bucket, Key=key)
        meta = head.get("Metadata", {})
        expected = labels_mod.object_labels(dep.dataset_uuid, entry["checksum_sha256"],
                                            dep.meta["sensitivity"], dep.user)
        for name, value in expected.items():
            if meta.get(name) != value:
                p.append("%s: label %s is %r, expected %r"
                         % (entry["path"], name, meta.get(name), value))
        if verify_checksums:
            actual, opening = _sha256_and_head(client, bucket, key,
                                               filetypes.SNIFF_BYTES)
            if actual != entry["checksum_sha256"]:
                p.append("%s: stored bytes hash to %s, manifest says %s"
                         % (entry["path"], actual[:12], entry["checksum_sha256"][:12]))
        elif entry["bytes"]:
            opening = _head_of_object(client, bucket, key, filetypes.SNIFF_BYTES)
        else:
            opening = b""
        # r13: a program under another name. Its name is step 7's.
        kind = filetypes.refused_content(opening)
        if kind and not filetypes.refused_name(entry["path"]):
            p.append("member content: %s starts like %s; not accepted"
                     % (entry["path"], kind))
    return total


def _check_member_paths(dep: Deposit, rep: Report) -> None:
    """Report member paths that are unusable, reserved or of a refused
    type, and warn about archives."""
    p = rep.problems
    for entry in dep.entries:
        err = keys.member_path_error(entry["path"])
        if err:
            p.append("member path: %s" % err)
        if keys.is_reserved_member(entry["path"]):
            p.append("member path %r is reserved" % entry["path"])
        # r13: only what this deposit brings; what is already in the
        # dataset is never judged here.
        kind = filetypes.refused_name(entry["path"])
        if kind:
            p.append("member type: %s is %s; not accepted" % (entry["path"], kind))
    archives = [e["path"] for e in dep.entries if filetypes.is_archive(e["path"])]
    if archives:
        rep.warnings.append("%d archive member(s) promoted unopened; contents "
                            "are not checked: %s"
                            % (len(archives), ", ".join(archives[:5])
                               + (" ..." if len(archives) > 5 else "")))


def _check_destination(dep: Deposit, client, bucket, rep: Report) -> None:
    """Read the record already at the destination, if there is one.
    Report one that cannot be built on; otherwise note the merge."""
    p = rep.problems
    dest_record_key = dep.prefix + "/" + keys.record_filename(dep.meta["dataset"])
    text, err = _read_json_object(client, bucket, dest_record_key)
    if text is not None:
        try:
            existing, upgraded = record.parse_record_with_status(text)
        except record.RecordParseError as e:
            p.append("destination record %s cannot be built on: %s"
                     % (dest_record_key, e))
        else:
            if upgraded:
                rep.warnings.append("destination record was schema 0.5; the "
                                    "merged record will be written as %s"
                                    % record.SCHEMA_VERSION)
            if (existing.get("identifier") != dep.prefix
                    or existing.get("dataset") != dep.meta["dataset"]):
                p.append("destination record's dataset/identifier (%r / %r) do not "
                         "match its location %s - moved or hand-edited"
                         % (existing.get("dataset"), existing.get("identifier"),
                            dep.prefix))
            else:
                rep.existing_record = existing
                _, added, updated = record.merge_manifest(existing.get("files"),
                                                          dep.entries)
                rep.warnings.append(
                    "destination already has dataset %s (%d files, modified %s); "
                    "merging: dataset_uuid %s kept, %d member(s) added, %d updated"
                    % (dep.meta["dataset"], len(existing.get("files", [])),
                       (existing.get("modified") or "?")[:10],
                       existing.get("dataset_uuid", "?")[:8], len(added), len(updated)))
    elif err != "absent":
        p.append("could not read destination record %s: %s" % (dest_record_key, err))


def check(dep: Deposit, store: DepositStore, cfg: PromoterConfig,
          vocab_terms: Set[str], domain_codes: List[str],
          verify_checksums: bool = True,
          vocab_doc: Optional[Dict] = None,
          provider_slugs: Optional[List[str]] = None,
          place_codes: Optional[Set[str]] = None) -> Report:
    """`vocab_doc`, when given, is the whole vocabulary (r9 authority
    file): a staged record whose subject terms have since been renamed,
    merged or retired is mapped and promoted with the mapping in its
    history; one whose term was split needs a person and is refused.
    `provider_slugs` and `place_codes` (r11) are the lists the record's
    provider and spatial are checked against."""
    rep = Report()
    client, bucket = store.client, store.bucket
    p = rep.problems

    # 1. control object state
    _check_control(dep, rep)
    if p:
        return rep

    staged_prefix = store.staged_key(dep.user, dep.id, dep.prefix)

    # 2. the staged record
    staged_rec = _read_staged_record(dep, client, bucket, staged_prefix, rep)
    staged_rec = _map_stale_subjects(staged_rec, vocab_doc, rep)
    rep.staged_record = staged_rec
    _resolve_derived_from(staged_rec, client, bucket, rep)
    _check_staged_record(staged_rec, dep, vocab_terms, domain_codes, vocab_doc,
                         provider_slugs, place_codes, rep)

    # 3. objects present at size
    for prob in deposit_logic.completion_problems(staged_prefix, dep.entries,
                                                  store.stored_size):
        p.append(prob)
    if p:
        return rep      # no point hashing or label-checking missing objects

    # 4 + 5 + 6. checksums, labels, sizes
    total = _check_members(dep, client, bucket, cfg, staged_prefix,
                           verify_checksums, rep)
    rep.total_bytes = total
    if cfg.max_deposit_bytes is not None and total > cfg.max_deposit_bytes:
        p.append("deposit totals %d bytes, over the %d-byte limit"
                 % (total, cfg.max_deposit_bytes))

    # 7. member paths (belt and braces)
    _check_member_paths(dep, rep)

    # 8. authorisation
    if not cfg.user_may_deposit_to(dep.user, dep.meta["strand"]):
        p.append("depositor %s is not authorised for strand %s"
                 % (dep.user, dep.meta["strand"]))

    # 9. destination record
    _check_destination(dep, client, bucket, rep)

    return rep
