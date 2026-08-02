# Explicit limitations

- Offline use covers downloaded official catalog data, inventories, search,
  exports, page links, and offline mapping strategies. Live user collections and
  API-only crosswalks require credentials.
- Rebrickable and LDraw identifiers overlap often, not universally. Missing
  official crosswalk data remains unresolved.
- Relationship records are reviewable candidates, not proof of physical,
  geometric, or appearance equivalence.
- Historical inventory versions remain stored, but public queries intentionally
  expose only the newest numeric version.
- Element and official inventory evidence establishes historical catalog use. It
  does not establish current manufacture, stock, market price, or availability.
- API v3 does not provide general MOCs, MOC inventories, B-models, sub-sets, or
  pricing. Alternate builds for a set are its only MOC surface.
- The library preserves upstream image URLs as metadata but never downloads,
  caches, renders, or opens them.
- Catalog search ranking uses a materialized CTE and requires the runtime
  SQLite to be 3.35 or newer (March 2021). Python's version does not guarantee
  the linked SQLite version; check `sqlite3.sqlite_version` when compatibility
  is uncertain.
- Opening a session resolves and verifies the active snapshot under the
  promotion lock, so concurrent opens serialize and may report the catalog as
  busy after `Config.lock_timeout` seconds.
