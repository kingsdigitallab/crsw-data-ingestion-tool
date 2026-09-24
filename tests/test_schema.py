"""dataset.schema.json is the contract artefact for CI and the future
gateway; runtime validation is hand-rolled in record.py (stdlib
constraint, r5 §7). These tests keep the two in step mechanically.

The jsonschema-backed tests are dev-only: they skip cleanly when the
package is absent, so a plain `python -m unittest` on a clean machine
stays green and stdlib-only.
"""
import json
import unittest
from pathlib import Path

import deposit
from crsw_deposit import keys
from crsw_deposit import record

try:
    import jsonschema
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "dataset.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def worked_example() -> dict:
    """The r6 worked example, assembled through the real builder."""
    return record.build_record(
        dataset_uuid="8f14e45f-ceea-467f-a34e-9db1c153f0a1",
        identifier="rs2/csac/green/2_final/csac-clean",
        strand="rs2", domain="quant", project="csac", dataset="csac-clean",
        state="2_final", sensitivity="green",
        temporal=record.temporal_object("1989", "2025-12-31"),
        version="3-0",
        abstract=" ".join(["word"] * 150),
        subject=["armed-conflict", "forced-labour"],
        vocabulary_version="2026-07-23",
        creator="CSAC coding team",
        source_type="archive",
        source_detail="CSAC coding project, University of Nottingham",
        license="CC-BY-4.0", steward="Kevin Fahey",
        depositors=["njakeman"],
        created="2026-07-29T10:15:00Z", modified="2026-07-29T10:15:00Z",
        derived_from=[{"kind": "dataset",
                       "identifier": "rs2/csac/amber/1_interim/csac-clean"}],
        category_history=[{"when": "2026-09-24T10:00:00Z", "by": "vocabulary",
                           "kind": "replaced", "from": ["debt-bondage"],
                           "to": ["forced-labour"],
                           "reason": "vocabulary change dated 2026-09-01",
                           "vocabulary_version": "2026-09-01"}],
        files=[
            record.manifest_entry("csac-clean-2025.csv", "e3" * 32,
                                  48211023, fmt="text/csv"),
            record.manifest_entry(
                "csac-annual-1989.csv", "ab" * 32, 220144,
                temporal=record.temporal_object("1989-01-01", "1989-12-31"),
                fmt="text/csv"),
        ])


class TestSchemaInStepWithCode(unittest.TestCase):
    """Stdlib-only: constants in the schema must equal the code's."""

    def test_enums_match_keys_constants(self):
        props = SCHEMA["properties"]
        self.assertEqual(tuple(props["strand"]["enum"]), keys.STRANDS)
        self.assertEqual(tuple(props["state"]["enum"]), keys.STATES)
        self.assertEqual(tuple(props["sensitivity"]["enum"]),
                         keys.SENSITIVITIES)

    def test_identifier_pattern_generated_from_keys_constants(self):
        # r6 §1: the dataset is a fifth path element.
        expected = "^(%s)/[a-z0-9-]+/(%s)/(%s)/[a-z0-9-]+$" % (
            "|".join(keys.STRANDS), "|".join(keys.SENSITIVITIES),
            "|".join(keys.STATES))
        self.assertEqual(SCHEMA["properties"]["identifier"]["pattern"],
                         expected)

    def test_dataset_property_matches_slug_rule(self):
        self.assertEqual(SCHEMA["properties"]["dataset"]["pattern"],
                         "^[a-z0-9-]+$")

    def test_source_type_enum_matches_cli(self):
        cli_codes = [code for _, code, _ in deposit.SOURCE_TYPE_ENTRIES]
        self.assertEqual(SCHEMA["properties"]["source_type"]["enum"],
                         cli_codes)

    def test_schema_versions_match_record(self):
        # r8 §5: the contract lists every version the tool reads; the
        # one it writes is among them.
        self.assertEqual(tuple(SCHEMA["properties"]["schema_version"]["enum"]),
                         record.ACCEPTED_SCHEMA_VERSIONS)
        self.assertIn(record.SCHEMA_VERSION, record.ACCEPTED_SCHEMA_VERSIONS)

    def test_required_matches_record_tiers(self):
        self.assertEqual(set(SCHEMA["required"]), set(record.REQUIRED_FIELDS))

    def test_every_field_tier_present_in_schema(self):
        props = set(SCHEMA["properties"])
        for field in (record.REQUIRED_FIELDS + record.RECOMMENDED_FIELDS
                      + record.OPTIONAL_FIELDS):
            self.assertIn(field, props)

    def test_manifest_entry_fields_present_in_schema(self):
        entry_props = set(SCHEMA["properties"]["files"]["items"]["properties"])
        for field in record.MANIFEST_REQUIRED + record.MANIFEST_OPTIONAL:
            self.assertIn(field, entry_props)
        self.assertEqual(
            SCHEMA["properties"]["files"]["items"]["required"],
            list(record.MANIFEST_REQUIRED))

    def test_reserved_path_refused_by_schema_text(self):
        # r6 §2: reservation is the dataset.*.json pattern, not one name.
        pattern = (SCHEMA["properties"]["files"]["items"]["properties"]
                  ["path"]["not"]["pattern"])
        self.assertTrue(keys.RESERVED_RECORD_RE.match("dataset.meta.json"))
        import re
        self.assertTrue(re.match(pattern, "dataset.meta.json"))
        self.assertTrue(re.match(pattern, "dataset.foo.json"))
        self.assertFalse(re.match(pattern, "dataset.json"))
        # r7: the pattern is segment-aware - a nested reserved name must
        # still be caught, since is_reserved_member checks every depth.
        self.assertTrue(re.search(pattern, "sub/dataset.foo.json"))
        self.assertTrue(re.search(pattern, "a/b/dataset.x.json"))
        self.assertFalse(re.search(pattern, "a/b/notes.json"))

    def test_temporal_shape_at_record_and_entry_level(self):
        for location in (SCHEMA["properties"]["temporal"],
                         SCHEMA["properties"]["files"]["items"]
                         ["properties"]["temporal"]):
            self.assertEqual(set(location["required"]), {"start", "end"})

    def test_abstract_has_no_length_constraint(self):
        # r6 §0: length guidance lives in the tool, not the contract.
        self.assertNotIn("minLength", SCHEMA["properties"]["abstract"])


@unittest.skipUnless(HAVE_JSONSCHEMA,
                     "jsonschema not installed (dev-only test)")
class TestEmittedRecordAgainstSchema(unittest.TestCase):
    def test_worked_example_validates(self):
        jsonschema.validate(worked_example(), SCHEMA)

    def test_runtime_validator_accepts_the_same(self):
        vocab_terms = {"armed-conflict", "forced-labour"}
        errors, _ = record.validate_record(worked_example(), vocab_terms)
        self.assertEqual(errors, [])

    def test_short_abstract_passes_both_tool_and_contract(self):
        # r6 §0: the schema carries no length constraint, so the tool's
        # lenience (warn, never block) cannot contradict the contract.
        rec = worked_example()
        rec["abstract"] = "2"
        jsonschema.validate(rec, SCHEMA)
        vocab_terms = {"armed-conflict", "forced-labour"}
        errors, warnings = record.validate_record(rec, vocab_terms)
        self.assertEqual(errors, [])
        self.assertTrue(any("words" in w for w in warnings))


@unittest.skipUnless(HAVE_JSONSCHEMA,
                     "jsonschema not installed (dev-only test)")
class TestRuntimeAtLeastAsStrict(unittest.TestCase):
    """Anything the schema rejects, validate_record must reject too - the
    runtime check may be stricter than the contract, never looser."""

    VOCAB_TERMS = {"armed-conflict", "forced-labour"}

    MUTATIONS = (
        ("bad uuid", {"dataset_uuid": "not-a-uuid"}),
        ("bad version", {"version": "v3"}),
        ("empty subject", {"subject": []}),
        ("empty files", {"files": []}),
        ("bad checksum", {"files": [{"path": "a.csv",
                                     "checksum_sha256": "zz", "bytes": 1}]}),
        ("negative bytes", {"files": [{"path": "a.csv",
                                       "checksum_sha256": "ab" * 32,
                                       "bytes": -1}]}),
        ("reserved path", {"files": [{"path": "dataset.meta.json",
                                      "checksum_sha256": "ab" * 32,
                                      "bytes": 1}]}),
        ("nested reserved path", {"files": [{"path": "sub/dataset.x.json",
                                             "checksum_sha256": "ab" * 32,
                                             "bytes": 1}]}),
        ("leading slash path", {"files": [{"path": "/a.csv",
                                           "checksum_sha256": "ab" * 32,
                                           "bytes": 1}]}),
        ("bad strand", {"strand": "rs9"}),
        ("missing created", {"created": None}),
        ("bad dataset slug", {"dataset": "Bad Slug"}),
        ("temporal missing end", {"temporal": {"start": "1989"}}),
        ("reference without kind", {"derived_from": [{"identifier": "x"}]}),
        ("dataset reference without identifier",
         {"derived_from": [{"kind": "dataset"}]}),
        ("external reference with nothing", {"derived_from": [{"kind": "external"}]}),
        ("activity without kind", {"provenance": [{"tool": {"name": "x"}}]}),
        ("tool without name", {"provenance": [{"activity": "clean",
                                               "tool": {"repo": "r"}}]}),
        ("history entry without kind", {"category_history": [
            {"when": "2026-09-24T10:00:00Z", "by": "k1"}]}),
        ("history entry with bad kind", {"category_history": [
            {"when": "2026-09-24T10:00:00Z", "by": "k1", "kind": "renamed"}]}),
    )

    def test_schema_invalid_is_runtime_invalid(self):
        for label, overrides in self.MUTATIONS:
            rec = worked_example()
            for field, value in overrides.items():
                if value is None:
                    del rec[field]
                else:
                    rec[field] = value
            with self.assertRaises(jsonschema.ValidationError, msg=label):
                jsonschema.validate(rec, SCHEMA)
            errors, _ = record.validate_record(rec, self.VOCAB_TERMS)
            self.assertTrue(errors, "runtime accepted: %s" % label)


if __name__ == "__main__":
    unittest.main()
