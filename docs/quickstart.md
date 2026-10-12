# Quickstart

Install the beta library with Python 3.12 or newer:

```console
uv add rebrickable
rebrickable status
rebrickable refresh
```

The refresh downloads and validates all 12 daily gzip CSV files, preserving the
archive bytes for storage and fingerprints. It builds a
unique snapshot and changes one atomic active pointer only after schema,
cross-file references, FTS, row counts, and SQLite integrity pass. A transient
mixed CDN generation is downloaded once more after a bounded delay; a second
failure leaves the old snapshot active.

Cancellation waits for active imports, filesystem operations, bridge writes,
and connection workers to finish before releasing their resources. An import
can finish in full, and a contended lock can wait until `Config.lock_timeout`.
Cancellation before the promotion worker starts keeps the previous snapshot
active. Once that worker starts, it finishes installing the snapshot, activating
the pointer, and pruning before raising `CancelledError`; the new complete
snapshot remains readable. Cancelled session cleanup closes the connection and
clears its pinned state, so a later explicit catalog operation can open a new
connection.

```python
from rebrickable import RebrickableSession, SearchKind

async with await RebrickableSession.open() as session:
    result = await session.search("3001 brick", kinds={SearchKind.PART})
    part = await session.parts.require(result.hits[0].canonical_id)
    inventory = await session.sets.inventory("10497-1")
```

Live API calls are deliberately separate and always receive an explicit key:

```python
from rebrickable import RebrickableClient

async with RebrickableClient(api_key=api_key) as client:
    part = await client.get_part("3001")
    async for lego_set in client.iter_sets(search="Galaxy Explorer"):
        print(lego_set.set_num, lego_set.name)
```

`Config.load()` reads YAML and then overlays `REBRICKABLE_API_KEY`. Normal
`Config.write()` calls omit the key. Persisting it requires
`include_secrets=True`, atomically rewrites the complete file, and applies mode
`0600` where supported. User tokens and passwords are never persisted.

## Snapshot retention

`snapshot_retention` counts confirmed activations in activation order. The
active pointer records private history without changing version-1 snapshots.
Legacy directories with unknown activation history and ambiguous candidates
are preserved outside that count, so disk use can exceed the configured limit
until those directories are reviewed and cleaned up manually. New managed
orphans are removed once the pointer confirms they are inactive.
