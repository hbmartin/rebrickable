# Offline catalog and domain model

Every domain result is an immutable, slotted dataclass. Identifiers with possible
leading zeroes remain strings. Repository facades expose stable `get`, `require`,
`count`, bounded `list`, and paged asynchronous `iter` methods for parts, sets,
minifigures, colors, themes, categories, and elements. Required lookups raise
`EntityNotFoundError`.

`inventory()` returns the newest numeric upstream inventory version with direct
parts, contained sets, and minifigures. `bill_of_materials()` recursively expands
those owners, multiplies quantities, excludes spares by default, aggregates by
part/color, and retains contribution paths. Cycles raise `InventoryCycleError`
with the complete owner path.

Pass `version=` to set/minifig `inventory()`, or use `session.inventories` for
arbitrary owners. `versions()` marks the active numeric version and `diff()`
returns typed regular/spare quantity deltas. `session.parts.used_in_sets()` and
`usage_stats()` aggregate newest inventories; relationship, variant, and
canonical-mold helpers preserve upstream relationship types. Theme lineage and
descendant queries are recursive but depth-bounded, and elements can be looked
up directly by part/color.

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
Whole-BOM validation resolves rows first and batches inventory evidence, avoiding
one complete latest-inventory scan per BOM row.

Unified search spans part, set, minifigure, theme, category, color, and element
documents. Its seven stable tiers are exact canonical ID, exact external ID,
canonical prefix, exact normalized name, name prefix, all-token FTS, and
substring. Ties use kind and canonical ID. Empty browsing requires a kind or
filter and every page is bounded.
`SearchFilters(include_subthemes=True, theme_id=...)` recursively includes theme
descendants.

`CatalogState` is entirely local and reports `READY`, `MISSING`, `UNREADABLE`,
`SCHEMA_MISMATCH`, or `IMPORT_REQUIRED`; it does not compare freshness over the
network.

`RebrickableSession.open()` remains lazy. Use `connect()` when startup must fail
immediately if the catalog cannot be opened. `current_catalog_state()` follows
the active pointer, while `opened_catalog_state()` reports the immutable
snapshot pinned to that session. `rebrickable catalog path` exposes the SQLite
path for intentional advanced read-only SQL workflows.
