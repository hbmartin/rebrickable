# Read-only TUI consumer contract

`pyldraw3-tui` remains a separate repository and should treat this library as its
data and behavior layer. It must not construct SQL, depend on API DTOs, import
private modules, mutate user collections, or fetch images.

A consumer opens one awaited session and holds it for the browsing screen:

```python
async with await RebrickableSession.open() as session:
    state = await session.state()
    results = await session.search(text, kinds=kinds, limit=50)
```

For first-run or user-requested refresh, pass a short non-blocking progress
callback to `session.refresh()`. The callback receives frozen `ProgressEvent`
values on the event-loop thread; it should enqueue UI updates and return quickly.
Cancellation is normal async task cancellation and never changes the active
snapshot.

Entity links come from `page_url` or the pure URL builders. Open Rebrickable page
links, not image metadata. Set and minifigure views use `inventory()` for the
upstream structure or `bill_of_materials()` for recursive aggregation.

An LDraw screen passes neutral BOM rows to `session.translate_bom()`, displays
resolved/ambiguous/unresolved states separately, and preserves both original
identifiers and candidate explanations. API enrichment, when a key is available,
must be an explicit user action and receives a separately constructed client.

The TUI is read-only: it exposes no token generation, personal collection writes,
list mutation, `sync_user_sets`, configuration secret persistence, or image fetch.
