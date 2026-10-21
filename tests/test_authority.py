"""r9: the term authority file. Validation, each change kind, mapping a
subject list forward (idempotent), and the optional hierarchy."""
import copy
import json
import unittest

from crsw_deposit import authority, vocab


def small():
    return {
        "vocabulary_version": "2026-07-23",
        "terms": [
            {"slug": "forced-labour", "facet": "practices", "label": "Forced labour",
             "status": "current", "since": "2026-07-23"},
            {"slug": "debt-bondage", "facet": "practices", "label": "Debt bondage",
             "status": "current", "since": "2026-07-23"},
            {"slug": "survey", "facet": "methods", "label": "Survey",
             "status": "current", "since": "2026-07-23"},
            {"slug": "osint", "facet": "methods", "label": "OSINT",
             "status": "current", "since": "2026-07-23"},
            {"slug": "maritime", "facet": "contexts", "label": "Maritime",
             "status": "current", "since": "2026-07-23"},
        ],
        "changes": [],
        "facets": {"practices": ["forced-labour", "debt-bondage"],
                   "methods": ["survey", "osint"],
                   "contexts": ["maritime"]},
    }


def change(kind, **fields):
    c = {"kind": kind, "date": "2026-10-01", "by": "k1078591"}
    c.update(fields)
    return c


class TestValidate(unittest.TestCase):
    def test_small_and_bundled_files_are_valid(self):
        self.assertEqual(authority.validate_authority(small()), [])
        bundled = json.loads(vocab.bundled_vocab_path().read_text(encoding="utf-8"))
        self.assertEqual(authority.validate_authority(bundled), [])
        self.assertEqual(len(bundled["terms"]), 34)
        self.assertEqual(bundled["changes"], [])

    def test_bundled_facets_equal_derivation(self):
        bundled = json.loads(vocab.bundled_vocab_path().read_text(encoding="utf-8"))
        self.assertEqual(bundled["facets"], authority.facets_from_terms(bundled))

    def assertRefused(self, doc, fragment):
        errors = authority.validate_authority(doc)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_rules(self):
        d = small(); d["terms"][0]["slug"] = "Forced Labour"
        self.assertRefused(d, "lower-case")
        d = small(); d["terms"].append(dict(d["terms"][0]))
        self.assertRefused(d, "appears twice")
        d = small(); del d["terms"][0]["label"]
        self.assertRefused(d, "label is missing")
        d = small(); d["terms"][0]["status"] = "gone"
        self.assertRefused(d, "current or retired")
        d = small(); d["terms"][0]["status"] = "retired"
        self.assertRefused(d, "until date")
        d = small(); d["terms"][0]["replaced_by"] = ["x"]
        self.assertRefused(d, "only a retired term")
        d = small(); d["terms"][0].update(status="retired", until="2026-10-01",
                                          replaced_by=["nope"])
        self.assertRefused(d, "unknown term")
        d = small(); d["facets"]["practices"] = ["forced-labour"]
        self.assertRefused(d, "does not match")
        d = small(); d["vocabulary_version"] = "v1"
        self.assertRefused(d, "vocabulary_version")

    def test_providers_block_is_validated(self):
        d = small(); d["providers"] = [{"slug": "acled", "name": "ACLED",
                                       "url": "https://acleddata.com/",
                                       "rights": "ACLED terms"}]
        self.assertEqual(authority.validate_authority(d), [])
        self.assertEqual(list(authority.providers_of(d)), ["acled"])
        d = small(); d["providers"] = "acled"
        self.assertRefused(d, "providers must be a list")
        d = small(); d["providers"] = [{"slug": "ACLED", "name": "x"}]
        self.assertRefused(d, "lower-case")
        d = small(); d["providers"] = [{"slug": "acled"}]
        self.assertRefused(d, "name is missing")
        d = small(); d["providers"] = [{"slug": "acled", "name": "x"},
                                       {"slug": "acled", "name": "y"}]
        self.assertRefused(d, "appears twice")
        d = small(); d["providers"] = [{"slug": "acled", "name": "x", "url": "acleddata.com"}]
        self.assertRefused(d, "url must start")
        d = small(); d["providers"] = [{"slug": "acled", "name": "x", "rights": 5}]
        self.assertRefused(d, "rights must be a string")
        bundled = json.loads(vocab.bundled_vocab_path().read_text(encoding="utf-8"))
        self.assertIn("acled", authority.providers_of(bundled))

    def test_dump_keeps_providers_in_place(self):
        d = small(); d["domains"] = [{"code": "quant", "label": "Q", "steward": "K"}]
        d["providers"] = [{"slug": "acled", "name": "ACLED"}]
        text = authority.dump_authority(d)
        self.assertLess(text.index('"domains"'), text.index('"providers"'))
        self.assertLess(text.index('"providers"'), text.index('"terms"'))
        self.assertEqual(json.loads(text)["providers"], d["providers"])

    def test_change_rules(self):
        d = small(); d["changes"] = [change("merge", **{"from": ["x"], "to": ["forced-labour"]})]
        self.assertRefused(d, "unknown term")
        d = small(); d["changes"] = [change("split", **{"from": ["survey"], "to": ["osint"]})]
        self.assertRefused(d, "two or more")
        d = small(); d["changes"] = [change("retire", **{"from": ["survey"]}, date="2026-10-02"),
                                     change("retire", **{"from": ["osint"]}, date="2026-10-01")]
        self.assertRefused(d, "appended in order")
        d = small(); d["changes"] = [change("move", **{"from": ["survey"]})]
        self.assertRefused(d, "needs broader")
        d = small(); d["changes"] = [{"kind": "add", "to": ["survey"], "date": "2026-10-01"}]
        self.assertRefused(d, "by (who decided)")

    def test_hierarchy_rules(self):
        d = small(); d["terms"][1]["broader"] = "survey"
        self.assertRefused(d, "another facet")
        d = small(); d["terms"][1]["broader"] = "nope"
        self.assertRefused(d, "unknown term")
        d = small(); d["terms"][0]["broader"] = "debt-bondage"; d["terms"][1]["broader"] = "forced-labour"
        self.assertRefused(d, "loops")
        d = small(); d["terms"][0].update(status="retired", until="2026-10-01")
        d["terms"][1]["broader"] = "forced-labour"; d["facets"]["practices"] = ["debt-bondage"]
        self.assertRefused(d, "is retired")

    def test_scope_and_alias_rules(self):
        def errors_with(**fields):
            d = small()
            d["terms"][0].update(fields)
            return authority.validate_authority(d)

        self.assertEqual(errors_with(scope="Work exacted under menace of penalty."), [])
        self.assertEqual(errors_with(aliases=["forced-work", "compulsory-labour"]), [])
        for bad in ("", "   ", 3):
            self.assertIn("term forced-labour: scope must be a non-empty string",
                          errors_with(scope=bad))
        self.assertIn("term forced-labour: aliases must be a list of slugs",
                      errors_with(aliases="forced-work"))
        self.assertIn("term forced-labour: alias 'Forced Work' is not lower-case words "
                      "joined by hyphens", errors_with(aliases=["Forced Work"]))
        self.assertIn("term forced-labour: aliases repeat", errors_with(aliases=["a-b", "a-b"]))
        self.assertIn("term forced-labour: alias debt-bondage is a current term's slug",
                      errors_with(aliases=["debt-bondage"]))
        d = small()
        d["terms"][0]["aliases"] = ["bonded"]
        d["terms"][1]["aliases"] = ["bonded"]
        self.assertIn("term debt-bondage: alias bonded is also on forced-labour",
                      authority.validate_authority(d))
        # A retired term keeps its description; its alias may even be a
        # retired slug, which is the natural case after a rename.
        d = authority.apply_change(small(), change(
            "rename", **{"from": ["debt-bondage"], "to": ["bonded-labour"]}))
        authority.terms_by_slug(d)["debt-bondage"]["scope"] = "What it meant."
        authority.terms_by_slug(d)["bonded-labour"]["aliases"] = ["debt-bondage"]
        self.assertEqual(authority.validate_authority(d), [])

    def test_flat_file_is_not_an_authority(self):
        flat = {"vocabulary_version": "x", "facets": {"a": ["b"]}}
        self.assertFalse(authority.is_authority(flat))
        self.assertEqual(authority.facets_of(flat), {"a": ["b"]})
        self.assertIs(authority.normalise(flat), flat)
        self.assertEqual(authority.map_subjects(["b"], flat), (["b"], []))


class TestApplyChange(unittest.TestCase):
    def test_input_is_untouched(self):
        d = small(); before = copy.deepcopy(d)
        authority.apply_change(d, change("retire", **{"from": ["maritime"]}))
        self.assertEqual(d, before)

    def test_add_with_and_without_parent(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="OSINT: social",
            broader="osint"))
        t = authority.terms_by_slug(d)["osint-social"]
        self.assertEqual(t, {"slug": "osint-social", "facet": "methods",
                             "label": "OSINT: social", "status": "current",
                             "since": "2026-10-01", "broader": "osint"})
        self.assertEqual(d["facets"]["methods"], ["survey", "osint", "osint-social"])
        self.assertEqual(d["vocabulary_version"], "2026-10-01")
        self.assertEqual(len(d["changes"]), 1)
        d = authority.apply_change(d, change("add", to=["mining"], facet="contexts",
                                             label="Mining", date="2026-10-02"))
        self.assertNotIn("broader", authority.terms_by_slug(d)["mining"])
        self.assertEqual(d["vocabulary_version"], "2026-10-02")

    def test_add_puts_scope_and_aliases_on_the_term_not_the_change(self):
        d = authority.apply_change(small(), change(
            "add", to=["servile-marriage"], facet="practices", label="Servile marriage",
            scope="Marriage under the 1956 Convention art. 1(c). ",
            aliases=["slave-marriage"]))
        t = authority.terms_by_slug(d)["servile-marriage"]
        self.assertEqual(t["scope"], "Marriage under the 1956 Convention art. 1(c).")
        self.assertEqual(t["aliases"], ["slave-marriage"])
        self.assertNotIn("scope", d["changes"][-1])
        self.assertNotIn("aliases", d["changes"][-1])
        with self.assertRaises(authority.AuthorityError):
            authority.apply_change(small(), change(
                "add", to=["x"], facet="practices", label="X", aliases=["survey"]))

    def test_rename_keeps_facet_parent_and_children(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="x", broader="osint"))
        d = authority.apply_change(d, change("rename", **{"from": ["osint"], "to": ["open-source"]},
                                             label="Open source", date="2026-10-02"))
        by = authority.terms_by_slug(d)
        self.assertEqual(by["osint"]["status"], "retired")
        self.assertEqual(by["osint"]["replaced_by"], ["open-source"])
        self.assertEqual(by["osint"]["until"], "2026-10-02")
        self.assertEqual(by["open-source"]["facet"], "methods")
        self.assertEqual(by["osint-social"]["broader"], "open-source")

    def test_merge_split_retire(self):
        d = authority.apply_change(small(), change(
            "merge", **{"from": ["debt-bondage"], "to": ["forced-labour"]}))
        self.assertEqual(authority.terms_by_slug(d)["debt-bondage"]["replaced_by"],
                         ["forced-labour"])
        self.assertEqual(d["facets"]["practices"], ["forced-labour"])
        d = authority.apply_change(d, change(
            "add", to=["survey-household"], facet="methods", label="a", date="2026-10-02"))
        d = authority.apply_change(d, change(
            "add", to=["survey-online"], facet="methods", label="b", date="2026-10-02"))
        d = authority.apply_change(d, change(
            "split", **{"from": ["survey"], "to": ["survey-household", "survey-online"]},
            date="2026-10-03"))
        self.assertEqual(authority.terms_by_slug(d)["survey"]["replaced_by"],
                         ["survey-household", "survey-online"])
        d = authority.apply_change(d, change("retire", **{"from": ["maritime"]},
                                             date="2026-10-04"))
        t = authority.terms_by_slug(d)["maritime"]
        self.assertEqual(t["status"], "retired")
        self.assertNotIn("replaced_by", t)
        self.assertEqual(d["facets"]["contexts"], [])
        self.assertEqual(authority.validate_authority(d), [])

    def test_refusals(self):
        cases = [
            (change("merge", **{"from": ["survey"], "to": ["forced-labour"]}), "another facet"),
            (change("merge", **{"from": ["survey"], "to": ["survey"]}), "itself"),
            (change("add", to=["survey"], facet="methods", label="x"), "already exists"),
            (change("add", to=["Bad Slug"], facet="methods", label="x"), "lower-case"),
            (change("add", to=["x"], facet="methods", label="x", broader="forced-labour"),
             "in facet practices"),
            (change("retire", **{"from": ["nope"]}), "unknown term"),
            (change("move", **{"from": ["survey"]}, broader="forced-labour"), "another facet"),
            (change("split", **{"from": ["survey"], "to": ["osint"]}), "two or more"),
            (change("move", **{"from": ["survey"]}, broader="survey"), "loop"),
            (change("retire", **{"from": ["survey"]}, by=""), "by (who decided)"),
            ({"kind": "explode", "by": "k"}, "not one of"),
        ]
        for c, fragment in cases:
            with self.assertRaises(authority.AuthorityError, msg=fragment) as cm:
                authority.apply_change(small(), c)
            self.assertIn(fragment, str(cm.exception))

    def test_retired_term_cannot_change_again(self):
        d = authority.apply_change(small(), change("retire", **{"from": ["maritime"]}))
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.apply_change(d, change("retire", **{"from": ["maritime"]},
                                             date="2026-10-02"))
        self.assertIn("already retired", str(cm.exception))

    def test_earlier_dated_change_is_refused(self):
        d = authority.apply_change(small(), change("retire", **{"from": ["maritime"]}))
        with self.assertRaises(authority.AuthorityError):
            authority.apply_change(d, change("retire", **{"from": ["survey"]},
                                             date="2026-09-01"))

    def test_move_up_and_down_and_refuse_retiring_a_parent(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="x", broader="osint"))
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.apply_change(d, change("retire", **{"from": ["osint"]},
                                             date="2026-10-02"))
        self.assertIn("narrower terms", str(cm.exception))
        d = authority.apply_change(d, change("move", **{"from": ["osint-social"]},
                                             broader=None, date="2026-10-02"))
        self.assertNotIn("broader", authority.terms_by_slug(d)["osint-social"])
        d = authority.apply_change(d, change("move", **{"from": ["osint"]},
                                             broader="osint-social", date="2026-10-03"))
        self.assertEqual(authority.terms_by_slug(d)["osint"]["broader"], "osint-social")
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.apply_change(d, change("move", **{"from": ["osint-social"]},
                                             broader="osint", date="2026-10-04"))
        self.assertIn("loop", str(cm.exception))


class TestMapSubjects(unittest.TestCase):
    def evolved(self):
        d = authority.apply_change(small(), change(
            "merge", **{"from": ["debt-bondage"], "to": ["forced-labour"]}))
        d = authority.apply_change(d, change(
            "add", to=["survey-household"], facet="methods", label="a", date="2026-10-02"))
        d = authority.apply_change(d, change(
            "add", to=["survey-online"], facet="methods", label="b", date="2026-10-02"))
        d = authority.apply_change(d, change(
            "split", **{"from": ["survey"], "to": ["survey-household", "survey-online"]},
            date="2026-10-03"))
        d = authority.apply_change(d, change("retire", **{"from": ["maritime"]},
                                             date="2026-10-04"))
        d = authority.apply_change(d, change("move", **{"from": ["survey-online"]},
                                             broader="survey-household", date="2026-10-05"))
        return d

    def test_merge_split_retire_as_decided(self):
        subjects, applied = authority.map_subjects(
            ["debt-bondage", "survey", "maritime", "osint"], self.evolved())
        self.assertEqual(subjects, ["forced-labour", "survey-household",
                                    "survey-online", "osint"])
        self.assertEqual([a["kind"] for a in applied],
                         ["replaced", "split_review", "removed"])
        self.assertEqual(applied[0], {"kind": "replaced", "from": ["debt-bondage"],
                                      "to": ["forced-labour"], "date": "2026-10-01"})
        self.assertEqual(applied[1]["to"], ["survey-household", "survey-online"])
        self.assertEqual(applied[2], {"kind": "removed", "from": ["maritime"],
                                      "to": [], "date": "2026-10-04"})

    def test_mapping_twice_is_a_no_op(self):
        d = self.evolved()
        once, _ = authority.map_subjects(["debt-bondage", "survey", "maritime"], d)
        self.assertEqual(authority.map_subjects(once, d), (once, []))

    def test_current_list_is_untouched(self):
        d = self.evolved()
        self.assertEqual(authority.map_subjects(["forced-labour", "osint"], d),
                         (["forced-labour", "osint"], []))

    def test_add_and_move_never_alter_a_list(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="x", broader="osint"))
        d = authority.apply_change(d, change("move", **{"from": ["osint-social"]},
                                             broader=None, date="2026-10-02"))
        self.assertEqual(authority.map_subjects(["osint"], d), (["osint"], []))

    def test_merge_keeps_position_and_collapses_duplicates(self):
        d = authority.apply_change(small(), change(
            "merge", **{"from": ["debt-bondage"], "to": ["forced-labour"]}))
        subjects, applied = authority.map_subjects(
            ["survey", "debt-bondage", "forced-labour"], d)
        self.assertEqual(subjects, ["survey", "forced-labour"])
        self.assertEqual(applied[0]["from"], ["debt-bondage"])

    def test_retired_term_without_a_change_entry_is_still_mapped(self):
        d = small()
        d["terms"][1].update(status="retired", until="2026-10-01",
                             replaced_by=["forced-labour"])
        d["facets"]["practices"] = ["forced-labour"]
        self.assertEqual(authority.validate_authority(d), [])
        subjects, applied = authority.map_subjects(["debt-bondage"], d)
        self.assertEqual(subjects, ["forced-labour"])
        self.assertEqual(applied[0]["kind"], "replaced")


class TestDescribeTerm(unittest.TestCase):
    """A scope note or alias is description, not identity: no change
    entry, the version stays, the input is untouched."""

    def test_scope_on_a_current_term(self):
        before = small()
        d = authority.describe_term(before, "forced-labour", scope="  Work exacted.  ")
        self.assertEqual(authority.terms_by_slug(d)["forced-labour"]["scope"], "Work exacted.")
        self.assertEqual(d["changes"], [])
        self.assertEqual(d["vocabulary_version"], "2026-07-23")
        self.assertEqual(before, small())
        self.assertEqual(authority.validate_authority(d), [])

    def test_aliases_add_deduplicate_and_clear(self):
        d = authority.describe_term(small(), "forced-labour", aliases=["forced-work"])
        d = authority.describe_term(d, "forced-labour", aliases=["forced-work", "unfree-labour"])
        self.assertEqual(authority.terms_by_slug(d)["forced-labour"]["aliases"],
                         ["forced-work", "unfree-labour"])
        d = authority.describe_term(d, "forced-labour", aliases=["slave-labour"],
                                    clear_aliases=True)
        self.assertEqual(authority.terms_by_slug(d)["forced-labour"]["aliases"], ["slave-labour"])
        d = authority.describe_term(d, "forced-labour", clear_aliases=True)
        self.assertNotIn("aliases", authority.terms_by_slug(d)["forced-labour"])

    def test_a_retired_term_can_be_described(self):
        d = authority.apply_change(small(), change("retire", **{"from": ["maritime"]}))
        d = authority.describe_term(d, "maritime", scope="What it meant.")
        self.assertEqual(authority.terms_by_slug(d)["maritime"]["scope"], "What it meant.")
        self.assertEqual(len(d["changes"]), 1)

    def test_refusals(self):
        for kwargs, message in (
                (dict(scope="x"), "unknown term 'dragons'"),
                (dict(), "nothing to do"),
                (dict(scope=""), "scope must be a non-empty string"),
                (dict(aliases=["debt-bondage"]), "is a current term's slug"),
                (dict(aliases=["Bad Alias"]), "not lower-case words")):
            slug = "dragons" if message.startswith("unknown") else "forced-labour"
            with self.assertRaises(authority.AuthorityError) as cm:
                authority.describe_term(small(), slug, **kwargs)
            self.assertIn(message, str(cm.exception))
        with self.assertRaises(authority.AuthorityError):
            authority.describe_term({"facets": {}}, "x", scope="y")

    def test_readers(self):
        d = authority.describe_term(small(), "forced-labour", scope="Work exacted.",
                                    aliases=["forced-work"])
        d = authority.apply_change(d, change("retire", **{"from": ["maritime"]}))
        authority.terms_by_slug(d)["maritime"]["aliases"] = ["sea"]   # retired: not offered
        self.assertEqual(authority.aliases_of(d), {"forced-work": "forced-labour"})
        self.assertEqual(authority.scopes_of(d), {"forced-labour": "Work exacted."})
        self.assertEqual(authority.labels_of(d)["osint"], "OSINT")
        self.assertEqual(authority.aliases_of({"facets": {}}), {})
        self.assertEqual(authority.scopes_of({"facets": {}}), {})
        self.assertEqual(authority.labels_of({"facets": {}}), {})


class TestHierarchyReads(unittest.TestCase):
    def test_flat_vocabulary_has_depth_zero_everywhere(self):
        d = small()
        self.assertEqual(authority.tree(d, "methods"), [("survey", 0), ("osint", 0)])
        self.assertEqual(authority.narrower(d, "osint"), [])
        bundled = json.loads(vocab.bundled_vocab_path().read_text(encoding="utf-8"))
        for facet, slugs in bundled["facets"].items():
            self.assertEqual(authority.tree(bundled, facet), [(s, 0) for s in slugs])

    def test_tree_and_narrower(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="x", broader="osint"))
        d = authority.apply_change(d, change(
            "add", to=["osint-social-video"], facet="methods", label="y",
            broader="osint-social", date="2026-10-02"))
        self.assertEqual(authority.tree(d, "methods"),
                         [("survey", 0), ("osint", 0), ("osint-social", 1),
                          ("osint-social-video", 2)])
        self.assertEqual(authority.narrower(d, "osint"),
                         ["osint-social", "osint-social-video"])
        self.assertEqual(authority.tree(d, "practices"),
                         [("forced-labour", 0), ("debt-bondage", 0)])


class TestExpandSubject(unittest.TestCase):
    """What a search for a slug should match: the term and everything
    under it; a retired slug, its successors."""

    def tree(self):
        d = authority.apply_change(small(), change(
            "add", to=["osint-social"], facet="methods", label="x", broader="osint"))
        return authority.apply_change(d, change(
            "add", to=["osint-social-video"], facet="methods", label="y",
            broader="osint-social", date="2026-10-02"))

    def test_flat_vocabulary_is_exact(self):
        self.assertEqual(authority.expand_subject(small(), "osint"), (["osint"], "exact"))

    def test_a_parent_gives_every_descendant_and_a_sibling_does_not(self):
        d = self.tree()
        self.assertEqual(authority.expand_subject(d, "osint"),
                         (["osint", "osint-social", "osint-social-video"], "narrower"))
        self.assertEqual(authority.expand_subject(d, "osint-social"),
                         (["osint-social", "osint-social-video"], "narrower"))
        self.assertEqual(authority.expand_subject(d, "survey"), (["survey"], "exact"))

    def test_a_renamed_slug_resolves_to_its_successor_expanded(self):
        d = authority.apply_change(self.tree(), change(
            "rename", date="2026-10-03", **{"from": ["osint"], "to": ["open-source"]}))
        self.assertEqual(authority.expand_subject(d, "osint"),
                         (["open-source", "osint-social", "osint-social-video"], "replaced"))

    def test_a_split_slug_gives_every_successor(self):
        d = authority.apply_change(small(), change(
            "add", to=["survey-online"], facet="methods", label="a"))
        d = authority.apply_change(d, change(
            "add", to=["survey-field"], facet="methods", label="b"))
        d = authority.apply_change(d, change(
            "split", **{"from": ["survey"], "to": ["survey-online", "survey-field"]}))
        self.assertEqual(authority.expand_subject(d, "survey"),
                         (["survey-online", "survey-field"], "replaced"))

    def test_a_retired_slug_with_no_successor_matches_nothing(self):
        d = authority.apply_change(small(), change("retire", **{"from": ["maritime"]}))
        self.assertEqual(authority.expand_subject(d, "maritime"), ([], "removed"))

    def test_ancestors_nearest_first(self):
        d = self.tree()
        self.assertEqual(authority.ancestors(d, "osint-social-video"), ["osint-social", "osint"])
        self.assertEqual(authority.ancestors(d, "osint"), [])
        self.assertEqual(authority.ancestors(d, "dragons"), [])

    def test_unknown_slug_and_a_pre_r9_file_are_matched_as_typed(self):
        self.assertEqual(authority.expand_subject(small(), "dragons"), (["dragons"], "exact"))
        flat = {"vocabulary_version": "x", "facets": {"methods": ["survey"]}}
        self.assertEqual(authority.expand_subject(flat, "survey"), (["survey"], "exact"))
        self.assertEqual(authority.expand_subject(None, "survey"), (["survey"], "exact"))


if __name__ == "__main__":
    unittest.main()
