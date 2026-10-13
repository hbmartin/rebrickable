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
The pointer-named previous snapshot participates even when its database or
manifest is unreadable; it ages out as later snapshots activate. If the
previous database becomes unreadable during refresh, the validated replacement
can still activate without its cached API mappings. User mappings from YAML
remain available, and the progress callback reports skipped cache carryover.
During explicit refresh, a hot SQLite rollback journal is recovered under the
promotion lock before copying cached mappings. Ordinary catalog reads remain
read-only. Temporary access failures, resource exhaustion, and storage errors
abort refresh and preserve the active pointer and mappings; they are not proof
of catalog corruption.
Structural snapshot damage, including missing files, nonregular contents and
symlink loops, permits replacement. Verification checks the required version-1
schema and snapshot identity as well as SQLite integrity. A hot-journal
diagnostic recommends explicit refresh; ordinary reads never repair it.
The directory name locates the snapshot; logical identity belongs to database
metadata and the manifest when present. An intact snapshot may be copied or
renamed with its pointer updated without rewriting matching logical identities.
Manifest damage does not by itself discard a safely contained, verified mapping
cache. An obstructed active-pointer destination fails before downloads, and
promotion checks it again before installing the candidate.
Pointer and activation-marker reads use one regular-file descriptor, with
nonblocking opening where supported, so a file-to-FIFO swap cannot block the
reader. Both are limited to 1 MiB and check growth after descriptor inspection;
small files do not reserve the full limit. Malformed or deeply nested JSON and
integers exceeding 4,300 digits are invalid, independently of Python's global
integer-conversion setting. An existing oversized pointer reports its path and
observed size and stops refresh for review without overwriting its history or
cached mappings. Refresh checks the exact prospective pointer encoding,
including its final LF, before downloads and again under the promotion lock
before carrying mappings or moving the candidate. Capacity errors include the
path, byte limit, encoded size, and history count. They stop refresh even if the
upstream datasets might be unchanged; review accumulated activation history
instead of truncating it. Unsafe, oversized, inaccessible, malformed or deeply
nested activation markers preserve uncertain directories without interrupting
pruning. An opening denial is structural only when a fresh probe confirms
damage; inaccessible regular files remain operational. Failure probes inspect
the link itself only when nofollow opening is supported and enabled; otherwise
they follow the target to match the opening behavior.
Pointer-parent checks stop at the filesystem root, and promotion rechecks the destination after
carryover and staging movement, immediately before installation.
Database verification is independent of manifest classification. An identity
disagreement keeps ordinary access unavailable and skips cached mappings; a
manifest-only schema disagreement may retain a separately verified version-1
cache. These identity checks also apply to partial manifests and to the
attached copy source after journal recovery. An unsupported database schema
reports `SCHEMA_MISMATCH`; opening it raises `CatalogSchemaError` with the found
and supported versions when available, and explicit refresh replaces it without
copying its mappings. Schema errors distinguish database versions from manifest
declarations. After journal recovery, a known identity mismatch skips another
attach; eligible sources still undergo the transaction's metadata check.
Healthy verification is reused only when the resolved database identity, size,
modification/change times, and journal/WAL sidecars are unchanged. Changes to
those signatures trigger verification again under the promotion lock before
copying. Stable corruption or unsupported-schema results freshly obtained under
that lock may also be reused there, but are not reused across downloads.
This optimization assumes immutable snapshots: external edits retaining the
same filesystem signatures, including timestamps on coarse-resolution
filesystems, may evade it. Attached source metadata is still checked inside the
copy transaction before inserting mappings. Missing
SQLite features, including modules, functions and collations, are
operational errors rather than proof of catalog corruption.
If rollback or detachment fails during cache-copy cleanup, ordinary cleanup
errors, including `MemoryError`, leave the original SQLite exception primary;
its notes identify the cleanup phase and retain the cleanup traceback. Cleanup
cancellation and exit exceptions, including `CancelledError`,
`KeyboardInterrupt`, and `SystemExit`, propagate with the SQLite error as
context. Recovery does not proceed after any failed cleanup.
Legacy directories with unknown activation history and ambiguous candidates
are preserved outside that count, so disk use can exceed the configured limit
until those directories are reviewed and cleaned up manually. New managed
orphans are removed once the pointer confirms they are inactive.
Uncertain historical paths keep their activation provenance for later cleanup
retries without occupying an available retention slot.

When a download server returns `304`, refresh checks that the cached archive is
a stable regular file contained within its snapshot and matches its recorded
byte length and SHA256. Contained links are supported. Missing, unsafe or changed
archives trigger one fresh request without conditional headers. Operational
source-read or staging-write failures abort refresh. Reused archives follow the
normal header and import validation; no additional gzip scan is performed.
Length mismatches are rejected before copying. Cached-file opens reject
nonregular replacements without blocking on a FIFO where the platform supports
nonblocking opens.
