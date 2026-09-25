"""Form-level metadata validation for a new deposit.

Mirrors the CLI interview (deposit.prompt_metadata) rule for rule, but
every rule is a call into crsw_deposit - nothing is re-implemented
here. The result is field -> message so the form can show errors
inline; the full-record check (record.validate_record) still runs at
finalise, when the manifest exists."""
from typing import Dict, List, Tuple

from crsw_deposit import deposit_logic, keys, record, vocab

PROVENANCE_FIELDS = ("provenance_activity", "provenance_tool",
                     "provenance_repo", "provenance_commit",
                     "provenance_description")

# Matches dataset.schema.json's source_type enum and the CLI's menu.
SOURCE_TYPES = ("archive", "survey", "scrape", "instrument", "partner",
                "derived", "other")

RED_MESSAGE = ("red-classified data must not enter shared storage; it "
               "belongs in the TRE. Contact the domain steward.")


def _text(form: Dict, name: str) -> str:
    value = form.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _list(form: Dict, name: str) -> List[str]:
    value = form.get(name)
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def default_license(sensitivity: str) -> str:
    """Same default the CLI offers: amber data is internal-only."""
    return "internal-only" if sensitivity == "amber" else "CC-BY-4.0"


def validate_meta(form: Dict, vocab_dict: Dict
                  ) -> Tuple[Dict, Dict[str, str], List[str]]:
    """Return (meta, errors, warnings). `meta` is the dict
    deposit_logic expects; it is complete only when errors is empty."""
    errors: Dict[str, str] = {}
    warnings: List[str] = []
    meta: Dict = {}

    strand = _text(form, "strand")
    if strand not in keys.STRANDS:
        errors["strand"] = "choose one of %s" % ", ".join(keys.STRANDS)
    meta["strand"] = strand

    sensitivity = _text(form, "sensitivity")
    if sensitivity == "red":
        errors["sensitivity"] = RED_MESSAGE
    elif sensitivity not in keys.SENSITIVITIES:
        errors["sensitivity"] = "choose green or amber"
    meta["sensitivity"] = sensitivity

    state = _text(form, "state")
    if state not in keys.STATES:
        errors["state"] = "choose one of %s" % ", ".join(keys.STATES)
    meta["state"] = state

    for field in ("project", "dataset"):
        raw = _text(form, field)
        slug = keys.normalise_project(raw)
        if not raw:
            errors[field] = "required"
        elif not slug:
            errors[field] = "nothing usable remains after normalising %r" % raw
        elif slug != raw:
            errors[field] = ("use lowercase letters, digits and hyphens: "
                             "%r would be stored as %r" % (raw, slug))
        meta[field] = slug

    domains = vocab.domains(vocab_dict)
    codes = [d["code"] for d in domains]
    domain = _text(form, "domain")
    if domain not in codes:
        errors["domain"] = "choose one of %s" % ", ".join(codes)
    else:
        chosen = next(d for d in domains if d["code"] == domain)
        steward = _text(form, "steward")
        if not steward and chosen["steward"] and chosen["steward"] != "TBC":
            steward = chosen["steward"]
        if steward:
            meta["steward"] = steward
    meta["domain"] = domain

    version = record.normalise_version(_text(form, "version") or "1-0")
    if version is None:
        errors["version"] = "two integers like 3-0 (3.0 and v3-0 are accepted)"
    meta["version"] = version

    for field in ("coverage_start", "coverage_end"):
        value = _text(form, field)
        err = record.coverage_error(value)
        if err:
            errors[field] = err
        meta[field] = value

    subjects = _list(form, "subject")
    if not subjects:
        errors["subject"] = "at least one subject term is required"
    else:
        unknown = record.unknown_subjects(subjects, vocab.all_terms(vocab_dict))
        if unknown:
            errors["subject"] = ("not in the vocabulary: %s. To propose an "
                                 "addition, open an issue on the vocabulary "
                                 "repo." % ", ".join(unknown))
    meta["subject"] = subjects

    abstract = _text(form, "abstract")
    if not abstract:
        errors["abstract"] = "required"
    else:
        warn = record.abstract_warning(abstract)
        if warn:
            warnings.append(warn)
    meta["abstract"] = abstract

    meta["license"] = _text(form, "license") or default_license(sensitivity)

    source_type = _text(form, "source_type")
    if source_type and source_type not in SOURCE_TYPES:
        errors["source_type"] = "choose one of %s" % ", ".join(SOURCE_TYPES)
    if source_type:
        meta["source_type"] = source_type
    detail = _text(form, "source_detail")
    if detail:
        if len(detail) < 10:
            warnings.append("source detail %r will not help anyone in five "
                            "years - consider naming the archive, URL, or "
                            "reference." % detail)
        meta["source_detail"] = detail

    creator = _text(form, "creator")
    if creator:
        meta["creator"] = creator

    # r8 §3: what this came from, one identifier or URL per line (or a
    # list), through the same rule as the CLI interview.
    raw = form.get("derived_from")
    refs = record.references_from_lines(raw if isinstance(raw, list) else
                                        (str(raw) if raw else ""))
    if refs:
        own = None
        if not any(f in errors for f in ("strand", "project", "sensitivity",
                                         "state", "dataset")):
            own = deposit_logic.dataset_prefix(meta)
        ref_errors = []
        for i, ref in enumerate(refs):
            ref_errors.extend(record.validate_reference(
                ref, "derived_from[%d]" % (i + 1), own))
        if ref_errors:
            errors["derived_from"] = "; ".join(ref_errors)
        meta["derived_from"] = refs
    elif source_type == "derived":
        warnings.append("source type is 'derived' but derived_from is "
                        "empty - derived from what?")

    # One provenance activity, if a script or notebook is named.
    activity = _text(form, "provenance_activity")
    tool_name = _text(form, "provenance_tool")
    if activity or tool_name:
        act: Dict = {"activity": activity}
        if not tool_name:
            errors["provenance_tool"] = "name the tool or script"
        else:
            tool: Dict = {"name": tool_name}
            for field in ("repo", "commit"):
                value = _text(form, "provenance_" + field)
                if value:
                    tool[field] = value
            act["tool"] = tool
        description = _text(form, "provenance_description")
        if description:
            act["description"] = description
        act_errors, act_warnings = record.validate_activity(
            act, "provenance[1]", set(), vocab.activity_codes(vocab_dict))
        if act_errors:
            errors["provenance_activity"] = "; ".join(act_errors)
        warnings.extend(act_warnings)
        meta["provenance"] = [act]

    meta["vocabulary_version"] = vocab_dict.get("vocabulary_version")
    return meta, errors, warnings
