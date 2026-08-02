# rebrickable

`rebrickable` is a typed asynchronous Python library and scriptable CLI for the
official Rebrickable catalog, API v3, inventories, bills of materials, and
LDraw cross-referencing.

The downloaded catalog is deliberately offline-first. After one explicit
refresh, search, inventory inspection, exports, URLs, and LDraw translation do
not require an API key and do not perform network I/O.

```console
uv add rebrickable
rebrickable refresh
rebrickable search "3001 brick 2 x 4"
rebrickable set 10497-1 --bom --json
```

```python
from rebrickable import RebrickableSession, SearchKind

async with await RebrickableSession.open() as session:
    result = await session.search("3001", kinds={SearchKind.PART})
    part = await session.parts.require(result.hits[0].canonical_id)
    print(part, part.page_url)
```

Live API calls use an explicit key and authenticate only through the
`Authorization` header:

```python
from rebrickable import RebrickableClient

async with RebrickableClient(api_key="...") as client:
    part = await client.get_part("3001")
```

See the [quickstart](docs/quickstart.md), [CLI reference](docs/cli.md),
[data model](docs/data-model.md), and [LDraw bridge](docs/ldraw-bridge.md) for
the complete contracts and limitations.

LEGO® is a trademark of the LEGO Group. Rebrickable is a trademark of
Rebrickable Pty Ltd. This project is not endorsed by either organization.
