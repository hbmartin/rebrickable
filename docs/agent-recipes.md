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

report = await session.translate_bom((LDrawBomItem("3001", 4, 2),))
if report.unresolved_count or report.ambiguous_count:
    review = translation_to_csv(report, unresolved_only=True)
```

## Page through API results

```python
async with RebrickableClient(api_key=api_key) as client:
    async for item in client.iter_parts(search="slope", inc_part_details=1):
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
