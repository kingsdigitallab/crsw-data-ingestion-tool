<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: the reasons and spec references taken out of the docstrings in crsw_deposit/places.py, kept as a list -->

# Decisions behind `crsw_deposit/places.py`

The docstrings in the module say what each function does and returns.
The reasons, and the specification each came from, are listed here. The
specifications are in `docs/specs/`; "r11 §4" means section 4 of
`DEPOSIT_TOOL_SPEC_R11.md`.

- **Decision:** places are the UN M49 geoscheme, as codes (`m49:NNN`, `iso:XX`) with a `broader` parent.
  **Reason:** a record's `spatial` can then be a list of codes that a search can widen or narrow.
  **Reference:** r11 §4.
- **Decision:** `places.json` is generated in the vocabulary repo from the UNSD CSV and never edited by hand.
  **Reason:** the file follows the published scheme; a hand edit would be lost at the next generation.
  **Reference:** r11 §4.
- **Decision:** the file reaches the tool by the vocabulary's own chain (fetch, cache, bundled), cached beside the vocabulary.
  **Reason:** one chain to understand. A fetch that fails or returns a bad file costs nothing but the timeout, because the bundled copy is always there; a fetched file that fails validation is ignored and the chain falls through.
  **Reference:** r11 §4.
- **Decision:** search expands the hierarchy at query time, not when a record is indexed (`expand_place`).
  **Reason:** coverage in a tree overlaps when one place is inside the other. A query for a country should find a dataset tagged with its subregion or the world, and a query for a region one tagged with a country inside it. Subject `broader` expansion works the same way.
  **Reference:** r11 §4.
- **Decision:** the hierarchy readers here mirror `authority`'s and do not reuse them.
  **Reason:** those are keyed on terms, facets and status; these on codes and kinds.
  **Reference:** r11 §4.
- **Decision:** `bundled_places_path` keeps its `sys._MEIPASS` fallback.
  **Reason:** a PyInstaller build adds the file beside `vocab.json` (`datas=[("crsw_deposit/places.json", "crsw_deposit")]`).
  **Reference:** `CLAUDE.md`, hard constraints.
