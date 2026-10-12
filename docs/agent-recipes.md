# Agent recipes

These recipes use bounded output and public imports only.

## Search and exact lookup

```python
result = await session.search("99780", kinds={SearchKind.PART}, limit=10)
part = await session.parts.require("99780")
```

## Flatten a set BOM

```python
rows = await session.sets.bill_of_materials("10497-1", include_spares=False)
for row in rows:
    print(row.part.part_num, row.color.id, row.quantity)
```

## Translate an LDraw BOM

```python
from rebrickable.exports import translation_to_csv

report = await session.translate_bom(
    (LDrawBomItem(part_num="3001", color_code=4, quantity=2),)
)
if report.unresolved_count or report.ambiguous_count:
    review = translation_to_csv(report, unresolved_only=True)
```

## Page through API results

```python
async with RebrickableClient(api_key=api_key) as client:
    async for item in client.iter_parts(search="slope", inc_part_details=True):
        process(item)
```

## Safely mutate a user list

```python
payload = PartListRequest(name="Agent review", is_buildable=False)
created = await client.create_user_part_list(payload, user_token=user_token)
```

Never put keys, passwords, or user tokens into generated snippets, arguments,
fixtures, logs, or exports. Do not call `sync_user_sets` unless the human has
explicitly chosen replacement and the call includes `confirm_replace=True`.

## Experimental garuff checks

The `Garuff (advisory)` workflow evaluates
[garuff](https://github.com/gahjelle/garuff) alongside the existing required
quality checks. Use the pinned command in the README from the repository root.
The checker requires Python 3.14; the package continues to support Python 3.12.
It runs through `uv tool run`, so it is not a package or development-group
dependency and does not change the lockfile.

The `[tool.garuff]` configuration excludes five conventions:

- `GAC001`: removing postponed annotations assumes Python 3.14 and conflicts
  with Python 3.12 support and typing-only imports.
- `GAC002`: requiring bases named `FrozenModel` or `StrictModel` rejects our
  `ApiModel` and `RequestModel`, which already use `ConfigDict(frozen=True)`.
- `GAC005`: replacing homogeneous tuples with lists changes the immutability
  and serialization contracts of public values.
- `GAC008`: changing established function signatures to keyword-only breaks
  callers and also flags pytest fixture parameters.
- `GAC010`: requiring docstrings on every private helper, special method,
  overload declaration, and test creates excessive boilerplate.

All other rules remain enabled for evaluation, including checks of generated
Python files. `GAC009` enforces keyword-only construction for internal records,
including `Parameter`, `Operation`, `DatasetDefinition`, and `CatalogPaths`.
Precise per-file exclusions preserve positional constructors for public records,
including configuration, progress events, domain values, and API exceptions.
Generated registry or query-type changes must go through their generator. Protocol bodies use descriptive docstrings (`GAC003`), and
docstrings use single backticks (`GAC004`). Avoid applying unreviewed automatic
fixes, especially after changing the active rules or pinned revision.

The workflow preserves full output and the process exit code in a
`garuff-report` artifact and adds a short run summary. A nonzero exit can mean
findings, parse/suppression errors, or a tool failure, including a crash with
exit `1`; the summary does not infer the cause from the status alone.
The entire job is advisory, including installation, configuration, and checker
failures. Check its step outcomes and logs even when required CI passes. Only
promote it to a required gate after reviewing the findings and its stability.

Initial evaluation on 2026-10-12 used rebrickable commit `597bca7` and garuff
revision `7d126c00f13b4d94f39f1107934089241ab7a6a1` (version `2026.7.0`).
The unconfigured run completed with 865 findings. The initial three exclusions
removed 145 findings, leaving these 720 advisory candidates across `src`, `scripts`,
and `tests`:

| Rule | Findings | Review consideration |
| --- | ---: | --- |
| `GAC003` | 6 | Protocol-body messages assume a docstring; these methods have none, so deleting the sole ellipsis would leave an invalid body. The fixer correctly skips them. |
| `GAC004` | 12 | Double backticks are valid reStructuredText; this is a prose convention. |
| `GAC008` | 137 | Includes fixtures and existing public signatures; keyword-only changes need compatibility review. |
| `GAC009` | 58 | Includes public values and generated dataclasses; constructor changes need compatibility review. |
| `GAC010` | 507 | Includes private helpers, special methods, and tests; these are documentation conventions. |

The tool installed successfully and did not crash in the initial runs. This
small sample does not establish stability, and the counts apply to the
committed baseline rather than uncommitted work in another checkout.

The current policy also disables `GAC008` and `GAC010`, retains keyword-only
internal records, and exempts public records from `GAC009` to preserve 1.x
compatibility. Protocol-body and docstring-formatting findings were addressed
without changing public signatures. The table records the initial evaluation;
it does not list current outstanding findings.
