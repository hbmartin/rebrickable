# Offline catalog and domain model

Every domain result is an immutable, slotted dataclass. Identifiers with possible
leading zeroes remain strings. Repository facades expose stable `get`, `require`,
and bounded `list` methods for parts, sets, minifigures, colors, themes,
categories, and elements. Required lookups raise `EntityNotFoundError`.

`inventory()` returns the newest numeric upstream inventory version with direct
parts, contained sets, and minifigures. `bill_of_materials()` recursively expands
those owners, multiplies quantities, excludes spares by default, aggregates by
part/color, and retains contribution paths. Cycles raise `InventoryCycleError`
with the complete owner path.

Use namespace-bearing references when identifiers cross systems:

```python
from rebrickable import ColorRef, PartRef

part = PartRef("ldraw", "99780")
color = ColorRef("ldraw", 4)
availability = await session.check_part_color(part, color)
```

Availability evidence distinguishes element IDs, official newest-inventory use,
spare-only use, and historical first/last years. It never claims current
production or market availability. `session.substitutes()` traverses relationship
records but labels results as evidence, not guaranteed physical equivalence.

Unified search spans part, set, minifigure, theme, category, color, and element
documents. Its seven stable tiers are exact canonical ID, exact external ID,
canonical prefix, exact normalized name, name prefix, all-token FTS, and
substring. Ties use kind and canonical ID. Empty browsing requires a kind or
filter and every page is bounded.

`CatalogState` is entirely local and reports `READY`, `MISSING`, `UNREADABLE`,
`SCHEMA_MISMATCH`, or `IMPORT_REQUIRED`; it does not compare freshness over the
network.
