"""r11 §4: places.json, the M49 hierarchy, through the vocabulary's
load chain, and the readers the form, the find page and validation
use."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from crsw_deposit import places

SMALL = {
    "places_version": "2026-10-21",
    "source": "test",
    "places": [
        {"code": "m49:001", "label": "World", "kind": "world"},
        {"code": "m49:019", "label": "Americas", "kind": "region", "broader": "m49:001"},
        {"code": "m49:142", "label": "Asia", "kind": "region", "broader": "m49:001"},
        {"code": "m49:419", "label": "Latin America and the Caribbean",
         "kind": "subregion", "broader": "m49:019"},
        {"code": "m49:005", "label": "South America", "kind": "intermediate",
         "broader": "m49:419"},
        {"code": "iso:CO", "label": "Colombia", "kind": "country",
         "broader": "m49:005", "m49": "170"},
        {"code": "iso:MX", "label": "Mexico", "kind": "country",
         "broader": "m49:013", "m49": "484"},
        {"code": "m49:013", "label": "Central America", "kind": "intermediate",
         "broader": "m49:419"},
        {"code": "iso:TH", "label": "Thailand", "kind": "country",
         "broader": "m49:035", "m49": "764"},
        {"code": "m49:035", "label": "South-eastern Asia", "kind": "subregion",
         "broader": "m49:142"},
    ],
}


def opener_returning(payload):
    def opener(url, timeout):
        return io.BytesIO(payload)
    return opener


def opener_failing(url, timeout):
    raise OSError("network down")


class TestValidate(unittest.TestCase):
    def assertRefused(self, doc, fragment):
        errors = places.validate_places(doc)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_small_and_bundled_are_valid(self):
        self.assertEqual(places.validate_places(SMALL), [])
        bundled = json.loads(places.bundled_places_path().read_text(encoding="utf-8"))
        self.assertEqual(places.validate_places(bundled), [])

    def test_rules(self):
        d = json.loads(json.dumps(SMALL)); d["places"][5]["code"] = "Colombia"
        self.assertRefused(d, "not m49:NNN or iso:XX")
        d = json.loads(json.dumps(SMALL)); d["places"].append(dict(d["places"][5]))
        self.assertRefused(d, "appears twice")
        d = json.loads(json.dumps(SMALL)); del d["places"][5]["label"]
        self.assertRefused(d, "label is missing")
        d = json.loads(json.dumps(SMALL)); d["places"][5]["kind"] = "planet"
        self.assertRefused(d, "kind must be one of")
        d = json.loads(json.dumps(SMALL)); d["places"][5]["broader"] = "m49:999"
        self.assertRefused(d, "unknown place")
        d = json.loads(json.dumps(SMALL)); d["places"][0]["broader"] = "iso:CO"
        self.assertRefused(d, "loops")
        d = json.loads(json.dumps(SMALL)); d["places_version"] = "v1"
        self.assertRefused(d, "places_version")
        self.assertRefused({"places_version": "2026-10-21", "places": []}, "non-empty")
        self.assertRefused([], "not a JSON object")


class TestLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.bundled = Path(self._tmp.name) / "places.json"
        self.bundled.write_text(json.dumps(SMALL), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_remote_used_and_cached(self):
        remote = dict(SMALL, places_version="2026-11-01")
        doc, source = places.load_places(
            cache=self.cache, bundled=self.bundled,
            opener=opener_returning(json.dumps(remote).encode("utf-8")))
        self.assertEqual((source, doc["places_version"]), ("remote", "2026-11-01"))
        cached = json.loads((self.cache / "places.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["places_version"], "2026-11-01")
        self.assertFalse((self.cache / "vocab.json").exists())

    def test_fetch_failure_falls_to_cache_then_bundled(self):
        doc, source = places.load_places(cache=self.cache, bundled=self.bundled,
                                         opener=opener_failing)
        self.assertEqual(source, "bundled")
        self.cache.mkdir()
        (self.cache / "places.json").write_text(
            json.dumps(dict(SMALL, places_version="2026-10-30")), encoding="utf-8")
        doc, source = places.load_places(cache=self.cache, bundled=self.bundled,
                                         opener=opener_failing)
        self.assertEqual((source, doc["places_version"]), ("cache", "2026-10-30"))

    def test_invalid_remote_is_ignored(self):
        bad = dict(SMALL, places=[{"code": "nope"}])
        doc, source = places.load_places(
            cache=self.cache, bundled=self.bundled,
            opener=opener_returning(json.dumps(bad).encode("utf-8")))
        self.assertEqual(source, "bundled")
        self.assertFalse((self.cache / "places.json").exists())


class TestReaders(unittest.TestCase):
    def test_codes_labels_and_tree(self):
        self.assertIn("iso:CO", places.codes(SMALL))
        self.assertEqual(places.labels_of(SMALL)["m49:419"],
                         "Latin America and the Caribbean")
        t = places.tree(SMALL)
        self.assertEqual(t[0], ("m49:001", 0))
        self.assertEqual(dict(t)["iso:CO"], 4)
        self.assertEqual(dict(t)["iso:TH"], 3)
        # Children follow their parent whatever the file order.
        codes_ = [c for c, _ in t]
        self.assertLess(codes_.index("m49:013"), codes_.index("iso:MX"))
        self.assertLess(codes_.index("m49:419"), codes_.index("m49:005"))
        self.assertEqual(places.tree(None), [])

    def test_ancestors_and_narrower(self):
        self.assertEqual(places.ancestors(SMALL, "iso:CO"),
                         ["m49:005", "m49:419", "m49:019", "m49:001"])
        self.assertEqual(places.ancestors(SMALL, "m49:001"), [])
        self.assertEqual(set(places.narrower(SMALL, "m49:419")),
                         {"m49:005", "iso:CO", "m49:013", "iso:MX"})
        self.assertEqual(places.narrower(SMALL, "iso:CO"), [])

    def test_expand_place(self):
        got, how = places.expand_place(SMALL, "iso:CO")
        self.assertEqual(how, places.EXPANDED_HIERARCHY)
        self.assertEqual(got, ["m49:001", "m49:019", "m49:419", "m49:005", "iso:CO"])
        got, how = places.expand_place(SMALL, "m49:419")
        self.assertEqual(got[:3], ["m49:001", "m49:019", "m49:419"])
        self.assertEqual(set(got[3:]), {"m49:005", "iso:CO", "m49:013", "iso:MX"})
        self.assertEqual(places.expand_place(SMALL, "iso:XX"), (["iso:XX"], "unknown"))
        self.assertEqual(places.expand_place(None, "iso:CO"), (["iso:CO"], "unknown"))
        lone = {"places_version": "2026-10-21",
                "places": [{"code": "iso:CO", "label": "C", "kind": "country"}]}
        self.assertEqual(places.expand_place(lone, "iso:CO"), (["iso:CO"], "exact"))


class TestBundled(unittest.TestCase):
    def test_bundled_places_hold_the_m49_hierarchy(self):
        doc = json.loads(places.bundled_places_path().read_text(encoding="utf-8"))
        self.assertEqual(places.validate_places(doc), [])
        self.assertEqual(places.ancestors(doc, "iso:CO"),
                         ["m49:005", "m49:419", "m49:019", "m49:001"])
        self.assertIn("iso:MX", places.narrower(doc, "m49:419"))
        self.assertIn("iso:TH", places.narrower(doc, "m49:142"))
        labels = places.labels_of(doc)
        self.assertEqual(labels["m49:419"], "Latin America and the Caribbean")
        self.assertEqual(labels["iso:AU"], "Australia")
        kinds = {p["kind"] for p in doc["places"]}
        self.assertEqual(kinds, set(places.KINDS))
        self.assertGreater(len(doc["places"]), 240)
        # Every country sits under the world.
        for p in doc["places"]:
            if p["kind"] == "country":
                self.assertEqual(places.ancestors(doc, p["code"])[-1], "m49:001", p)


if __name__ == "__main__":
    unittest.main()
