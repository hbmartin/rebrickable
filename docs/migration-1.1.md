# Migrating from 1.0 to 1.1

Version 1.1 expands the offline catalog into an analytical and historical data
layer, makes documented API query parameters statically checkable, sends true
bulk mutations where Rebrickable supports them, and exposes the same workflows
through a broader read-only CLI.

The release remains beta, async-only, GPL-3.0-or-later, and requires Python 3.12
or newer. The SQLite schema remains at version 1, so a catalog downloaded by
1.0 can be opened directly by 1.1. A refresh is optional unless newer upstream
data is wanted.

## Upgrade

Upgrade the distribution and confirm that the intended package is installed:

```console
uv add "rebrickable>=1.1,<2"
# or
python -m pip install --upgrade "rebrickable>=1.1,<2"

rebrickable --version
rebrickable catalog doctor
```

The similarly named `pyrebrickable` distribution is a separate project with
different imports and models. Remove it if `catalog doctor` reports a conflict:

```console
python -m pip uninstall pyrebrickable
```

Do not remove an application-data catalog during the package upgrade. Version
1.1 deliberately reads the version 1 database and manifest produced by 1.0.

## Compatibility summary

| Area | 1.0 behavior | 1.1 behavior | Action |
| --- | --- | --- | --- |
| Python API | Asynchronous | Asynchronous | No event-loop or facade migration |
| Catalog schema | Version 1 | Version 1 | Existing snapshots remain usable |
| Part-list sequence add | One request per item | One JSON-array request | Use the explicit sequential helper if partial progress matters |
| Set-list sequence add | One request per item | One JSON-array request | Use the explicit sequential helper if partial progress matters |
| API query keywords | Broad scalar `**query` typing | Per-operation generated `TypedDict` | Fix type-checker errors and use booleans for boolean flags |
| DTO additive fields | Selected values lived in `extra` | Stable values have named fields | Read the named attribute; do not expect it in `extra` |
| Inventory access | Newest version through set/minifig repositories | Newest or selected versions plus history and diffs | Existing calls still select newest |
| Session startup | `open()` was lazy | `open()` remains lazy; `connect()` is eager | Choose based on desired failure timing |
| CLI output | Per-command JSON/CSV switches | Global table/JSON/CSV/YAML plus legacy switches | Put global `--format` before the subcommand |

## Bulk mutation behavior

The main behavior change affects sequence inputs to
`add_user_part_list_parts()` and `add_user_set_list_sets()`. Version 1.0 sent
one form request for each record and raised `BatchMutationError` after the first
failed request. Version 1.1 sends the complete sequence as one JSON array, as
the upstream API permits.

The ordinary bulk form now looks like this:

```python
from rebrickable import PartListPartRequest

result = await client.add_user_part_list_parts(
    list_id,
    (
        PartListPartRequest(part_num="3001", color_id=4, quantity=2),
        PartListPartRequest(part_num="3002", color_id=1, quantity=1),
    ),
)

print(result.requested_count)
print(result.accepted_count)
print(result.skipped_count)
print(result.unaccepted_count)
```

`MutationResult` now retains four immutable record collections:

- `requested`: normalized records sent to Rebrickable;
- `accepted`: records confirmed or returned as accepted;
- `skipped`: records explicitly reported as skipped;
- `unaccepted`: rejected records, reported errors, or the unmatched request
  suffix when the response contains fewer outcomes than inputs.

Scalar inputs still use the existing form-encoded single-record request. User
set synchronization still requires `confirm_replace=True`; this protects the
replacement operation but is unrelated to synchronous Python APIs.

If a workflow relies on one-request-per-record ordering, retry boundaries, or
the accepted prefix carried by `BatchMutationError`, select that behavior
explicitly:

```python
result = await client.add_user_part_list_parts_sequential(list_id, requests)
result = await client.add_user_set_list_sets_sequential(list_id, requests)
```

The sequential helpers stop at the first failed item and raise
`BatchMutationError` with `accepted` and `failed_index`, matching the former
sequence behavior. Do not automatically replay either bulk or sequential
mutations without checking their result and the upstream account state.

## Typed live-API query keywords

Every public query wrapper now uses an operation-specific generated `TypedDict`
for `**kwargs`. Runtime validation still rejects unsupported parameters, but
type checkers can now catch misspellings and primitive-type mismatches before a
request is sent.

Boolean compatibility parameters should use booleans:

```python
# 1.0 code that may now fail static checking
part = await client.get_part("3001", inc_part_details=1)

# 1.1
part = await client.get_part("3001", inc_part_details=True)
```

The generated keyword contracts are in `rebrickable.api.query_types`. The
operation registry also preserves parameter location, upstream schema type,
required status, default, and description in `Operation.parameter_details`.
Both artifacts are regenerated from the vendored Swagger document and the
small reviewed compatibility overlay.

If application code forwards a dynamic dictionary, give the dictionary a
specific generated type where practical instead of suppressing checking:

```python
from rebrickable.api.query_types import LegoSetsListQuery

query: LegoSetsListQuery = {"search": "Galaxy Explorer", "page_size": 100}
page = await client.list_sets(**query)
```

## Response DTO field promotion

Additive response data is still preserved immutably in `ApiModel.extra`, but
documented stable fields now have named, typed attributes. Promotions include:

- part years, material, and `print_of` on `ApiPart`;
- element and part image URLs on `ApiElement`;
- element/design identifiers and part image URL on `ApiInventoryPart`;
- designer, MOC, image, part-count, theme, and year fields on
  `ApiAlternateBuild`;
- total and missing-part details on `ApiBuildResult`.

Code that read a promoted field from `extra` must use the named attribute:

```python
# 1.0
print_of = part.extra.get("print_of")

# 1.1
print_of = part.print_of
```

Because Pydantic serialization includes declared optional/default fields,
snapshots of `model_dump()` output may gain keys with `None` or empty values.
Update exact serialized-shape tests accordingly. Unknown future upstream fields
continue to be frozen into `extra`.

## Catalog analytics and relationships

All standard repositories now provide `count()` and bounded asynchronous
`iter(page_size=...)` in addition to `get()`, `require()`, and `list()`.

The parts repository adds offline analytical queries:

```python
usage = await session.parts.usage_stats("3001", color_id=4)
occurrences = await session.parts.used_in_sets(
    "3001", color_id=4, include_spares=False, limit=100, offset=0
)
relationships = await session.parts.relationships("3001")
variants = await session.parts.variants("3001")
canonical = await session.parts.canonical_mold("3001")
elements = await session.elements.for_part_color("3001", 4)
```

Usage and set-occurrence analytics intentionally use each owner's newest
numeric inventory version. Relationship-derived results retain the raw and
normalized upstream relationship type; they are candidates, not proof of
physical interchangeability.

Theme traversal is available through `lineage()` and `descendants()`. Search
can include an entire theme subtree:

```python
from rebrickable import SearchFilters, SearchKind

result = await session.search(
    "",
    kinds={SearchKind.SET},
    filters=SearchFilters(theme_id=721, include_subthemes=True),
)
```

Recursive relationship and theme operations are depth-bounded to protect
against malformed or cyclic upstream data.

## Inventory history and diffs

Existing calls remain compatible and select the newest numeric version:

```python
latest = await session.sets.inventory("10497-1")
```

Pass a version explicitly or use the new inventory repository:

```python
historical = await session.sets.inventory("10497-1", version=1)
versions = await session.inventories.versions("10497-1")
historical = await session.inventories.get("10497-1", version=1)
change = await session.inventories.diff("10497-1", 1, 2)
```

Each diff row separates regular and spare quantities and exposes
`quantity_delta` and `spare_quantity_delta`. Inventory history reflects
upstream catalog revisions; it does not establish production, stock, price, or
market availability.

## Session opening and snapshot state

`RebrickableSession.open()` remains lazy for compatibility. It constructs the
session, while the first catalog operation resolves, verifies, and pins the
active snapshot. Use `connect()` when missing or invalid catalog data should
fail during startup:

```python
async with await RebrickableSession.connect() as session:
    pinned = await session.opened_catalog_state()
```

State methods now distinguish two questions:

- `current_catalog_state(verify=False)` follows the currently active catalog
  pointer and is the explicit spelling of the compatible `state()` method;
- `opened_catalog_state()` reports the snapshot already pinned to the session.

A refresh can advance the active pointer. Long-running readers should use the
opened state when recording provenance for results from their pinned session.

## BOM validation performance

`validate_bom()` now resolves the input rows first and batches part/color
availability evidence into bounded SQLite queries. Return models and confidence
labels are unchanged, but code that inspects SQLite trace callbacks or assumes
one availability query per row must update those expectations.

Substitution traversal is still performed only where relationship-only evidence
requires it. Validation remains offline and never calls the live API
implicitly.

## CLI migration

The existing commands and their per-command `--json` and relevant `--csv`
switches remain available. Version 1.1 adds the global output selector:

```console
rebrickable --format json catalog doctor
rebrickable --format csv search brick --kind part --limit 50
rebrickable --format yaml part 3001 --usage
```

Global options must precede the subcommand. New command groups and modes are:

- `catalog path`, `catalog doctor`, `catalog versions`, and `catalog diff`;
- `bom normalize`, `bom diff`, and `bom validate`;
- read-only `api part|set|minifig|parts|sets|minifigs` lookups;
- `part --usage`, `part --sets` (with `--color-id`, `--include-spares`,
  `--limit`, and `--offset`), and `part --relationships`;
- `set|minifig --inventory --version N`;
- search year, theme/subtheme, part-count, category, material, and offset
  filters.

Flags a `part` mode does not use are rejected with invalid-input status instead
of being silently ignored, as is `--include-subthemes` without `--theme-id`.
`catalog versions` for an unknown owner exits with missing-data status rather
than printing an empty result. The `api` group requires `REBRICKABLE_API_KEY`
or a configured `api_key` and does not expose account mutations. `catalog
doctor` returns invalid-input status when it detects the conflicting
`pyrebrickable` distribution. Automation that asserts exit status should
account for these diagnostic results.

For intentional advanced read-only SQL, `catalog path` prints the active
snapshot database. Open that path read-only and do not modify package-managed
snapshots.

## Public imports and packaging

Mutation request models and `MutationResult` are now importable directly from
`rebrickable`, as are the new catalog analytics and inventory-history models.
Imports from their original submodules remain valid.

Source distributions now include the tests, documentation, and maintenance
scripts. Wheels remain focused on the importable package, type marker, and
vendored OpenAPI data.

## Recommended migration checks

Before deploying 1.1:

1. Run `rebrickable catalog doctor` in the target environment.
2. Confirm an existing 1.0 snapshot opens without refreshing.
3. Run both static checkers used by the application and replace integer API
   flags with booleans where reported.
4. Search for promoted field names under `.extra` and migrate them to typed
   attributes.
5. Review sequence calls to the two list-add methods and choose bulk or explicit
   sequential semantics deliberately.
6. Update exact DTO serialization snapshots for newly declared fields.
7. Exercise BOM validation against a representative large model and compare
   availability counts with the 1.0 result.
8. If CLI automation uses global output selection, place `--format` before the
   subcommand and verify exit-code handling for `catalog doctor`.

## Rollback

Rolling the Python distribution back to 1.0 does not require catalog conversion
because 1.1 does not change the SQLite schema. Stop active 1.1 sessions, install
the desired 1.0 release, and reopen the catalog normally.

Do not attempt to compensate for an uncertain user mutation by blindly
replaying it after rollback. Read the current upstream list or collection state
first, then apply a deliberate corrective mutation.
