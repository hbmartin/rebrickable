# LDraw bridge and BOMs

Core translation uses neutral `LDrawBomItem` and `LDrawColorInfo` records and does
not import `pyldraw3`. Install `rebrickable[ldraw]` for adapters built only on the
public `Model`, `BomRow`, `Colour`, `Parts`, `load_model`, and BOM APIs.

Part resolution order is user override, cached API LDraw ID, exact normalized
canonical ID, relationship candidates, then unresolved. Color resolution uses
override, cached API ID, unique RGB plus transparency, unique normalized name plus
transparency, then ambiguity/unresolved. Normalization changes slashes and case
and removes only a trailing `.dat`; it preserves meaningful decoration suffixes.

```python
from rebrickable import LDrawBomItem, RebrickableSession

items = (LDrawBomItem("3001.dat", 4, 2),)
async with await RebrickableSession.open() as session:
    report = await session.translate_bom(items)
```

No translation calls the API implicitly. `enrich_part_mapping()` and
`enrich_color_mapping()` are explicit and require a `RebrickableClient`; confirmed
crosswalks are serialized with catalog promotion and immediately become local
search fields. Enrichment authoritatively replaces previously cached mappings for
the same entity and system, so re-enriching repairs stale crosswalks; mappings
whose canonical id vanishes from a new snapshot are dropped during refresh.
YAML overrides are versioned, deterministic, and materialized into each snapshot.
Part `source_id` values may be written with or without the `.dat` suffix — they
are normalized on load — and color override identifiers must be integers.

`Bom.from_csv()` accepts `Part,Color,Quantity` with optional additional columns.
`Bom.from_rebrickable_csv()` accepts Rebrickable `part_num,color_id,quantity`
inventory/custom-list columns (plus their common aliases), and
`Bom.from_bricklink_xml()` accepts BrickLink wanted-list XML and preserves the
BrickLink part and color namespaces. Pass a `Path` for file input; strings are
always interpreted as inline content.
Quantities must be positive. Duplicate rows are recorded before aggregation;
normalization, diffs, validation, and Rebrickable custom-list CSV are stable.

Translation exports never replace original LDraw identifiers. They add resolved
Rebrickable identifiers, source strategies, candidates, explanations, and the
snapshot ID. JSON, RFC 4180 CSV, compact tables, and unresolved-only reports are
available.
