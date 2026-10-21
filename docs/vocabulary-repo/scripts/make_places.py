#!/usr/bin/env python3
"""places.json from the UNSD M49 overview CSV (r11 §4).

    python scripts/make_places.py UNSD_M49.csv places.json [--version 2026-10-21]

The CSV is the one the "CSV" button on
https://unstats.un.org/unsd/methodology/m49/overview/ downloads, with
its own column names (Global Code, Region Code, Sub-region Code,
Intermediate Region Code, Country or Area, M49 Code, ISO-alpha2 Code,
...). Comma or semicolon separated, either is read.

One entry per place. Codes: `m49:NNN` for the world, regions,
subregions and intermediate regions, `iso:XX` (ISO 3166 alpha-2) for a
country or area; a country or area the scheme gives no alpha-2 code
(Channel Islands, Sark) keeps its `m49:NNN`. `broader` is the parent:
the nearest of intermediate region, subregion, region, world. A
country's numeric M49 code is kept as `m49` for reference; it is never
a record value. Entries are grouped by kind, labels in order, so a
regeneration against an unchanged CSV is a no-op diff.

Stdlib only; no tool import, so the vocabulary repo can run it on its
own. The deposit tool validates the result with
`crsw_deposit.places.validate_places`."""
import argparse
import csv
import datetime
import json
import sys

SOURCE = "UNSD M49 overview CSV, https://unstats.un.org/unsd/methodology/m49/overview/"
KINDS = ("world", "region", "subregion", "intermediate", "country")


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        return list(csv.DictReader(f, delimiter=delimiter))


def _col(row, name):
    for key, value in row.items():
        if key and key.strip().lower() == name.lower():
            return (value or "").strip()
    raise SystemExit("column %r not found; is this the UNSD M49 CSV?" % name)


def places_from_rows(rows):
    nodes = {}

    def node(code, label, kind, broader, m49=None):
        if code in nodes:
            return
        entry = {"code": code, "label": label, "kind": kind}
        if broader:
            entry["broader"] = broader
        if m49:
            entry["m49"] = m49
        nodes[code] = entry

    for row in rows:
        world = "m49:" + _col(row, "Global Code")
        node(world, _col(row, "Global Name"), "world", None)
        parent = world
        for code_col, name_col, kind in (("Region Code", "Region Name", "region"),
                                         ("Sub-region Code", "Sub-region Name", "subregion"),
                                         ("Intermediate Region Code",
                                          "Intermediate Region Name", "intermediate")):
            code = _col(row, code_col)
            if code:
                code = "m49:" + code.zfill(3)
                node(code, _col(row, name_col), kind, parent)
                parent = code
        m49 = _col(row, "M49 Code").zfill(3)
        iso = _col(row, "ISO-alpha2 Code").upper()
        label = _col(row, "Country or Area")
        if iso:
            node("iso:" + iso, label, "country", parent, m49=m49)
        else:
            node("m49:" + m49, label, "country", parent)
    rank = {kind: i for i, kind in enumerate(KINDS)}
    return sorted(nodes.values(), key=lambda e: (rank[e["kind"]], e["label"], e["code"]))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv")
    p.add_argument("out")
    p.add_argument("--version", default=datetime.date.today().isoformat(),
                   help="places_version, a date (default today)")
    args = p.parse_args(argv)
    places = places_from_rows(_read(args.csv))
    doc = {"places_version": args.version, "source": SOURCE, "places": places}
    lines = ["{", '  "places_version": %s,' % json.dumps(doc["places_version"]),
             '  "source": %s,' % json.dumps(doc["source"]), '  "places": [']
    lines += ["    %s%s" % (json.dumps(e, ensure_ascii=False),
                            "," if i < len(places) - 1 else "")
              for i, e in enumerate(places)]
    lines += ["  ]", "}", ""]
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    kinds = {}
    for e in places:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("%s: %d places (%s)" % (args.out, len(places),
                                  ", ".join("%d %s" % (kinds[k], k) for k in KINDS if k in kinds)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
