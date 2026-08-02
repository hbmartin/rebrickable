# rebrickable

[![PyPI](https://img.shields.io/pypi/v/rebrickable.svg)](https://pypi.org/project/rebrickable/)
[![Python](https://img.shields.io/pypi/pyversions/rebrickable.svg)](https://pypi.org/project/rebrickable/)
[![Lint and test](https://github.com/hbmartin/rebrickable/actions/workflows/lint-test.yml/badge.svg)](https://github.com/hbmartin/rebrickable/actions/workflows/lint-test.yml)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A597%25-brightgreen)](https://github.com/hbmartin/rebrickable/actions/workflows/lint-test.yml)
[![Docs](https://img.shields.io/badge/docs-hbmartin.github.io-blue)](https://hbmartin.github.io/rebrickable/)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://github.com/hbmartin/rebrickable/blob/main/LICENSE.txt)

`rebrickable` is a typed asynchronous Python library and scriptable CLI for the
official Rebrickable catalog, API v3, inventories, bills of materials, and
LDraw cross-referencing.

The downloaded catalog is deliberately offline-first. After one explicit
refresh, search, inventory inspection, exports, URLs, and LDraw translation do
not require an API key and do not perform network I/O.

## Install

Requires Python 3.12 or newer.

```console
uv add rebrickable          # or: pip install rebrickable
uv add "rebrickable[ldraw]" # adds pyldraw3 for the LDraw bridge
```

## Quickstart

One explicit refresh downloads and validates the 12 daily catalog datasets
(~18 MB gzipped, ~275 MB as a local SQLite snapshot under your platform
application-data directory). Everything below it is offline.

```console
$ rebrickable refresh
[download] parts.csv.gz 1058667
[validate] parts
[import] inventory_parts 1547623
[promote] promoting snapshot
[done] catalog ready

$ rebrickable search "brick 2 x 4" --kind part --limit 3
part           3001                 Brick 2 x 4
part           3001a                Brick 2 x 4 without Cross Supports
part           3001apr0001          Brick 2 x 4 without Cross Supports with 4 Plane Windows in Thin Red Stripe Print

$ rebrickable set 10497-1
Set(set_num='10497-1', name='Galaxy Explorer', year=2022, theme_id=721, num_parts=1254, image_url='https://cdn.rebrickable.com/media/sets/10497-1.jpg')

$ rebrickable set 10497-1 --bom --csv | head -4
part_num,color_id,quantity
10247,71,6
11203,15,2
11211,71,6
```

The same catalog from Python:

```python
import asyncio

from rebrickable import RebrickableSession, SearchKind


async def main() -> None:
    async with await RebrickableSession.open() as session:
        result = await session.search("3001", kinds={SearchKind.PART})
        part = await session.parts.require(result.hits[0].canonical_id)
        print(part.name, part.page_url)
        # Brick 2 x 4 https://rebrickable.com/parts/3001/

        bom = await session.sets.bill_of_materials("10497-1")
        print(len(bom.rows), "distinct part/color rows")
        # 292 distinct part/color rows


asyncio.run(main())
```

## Offline vs. API key

| Works offline after `refresh`                         | Requires an API key                                      |
| ----------------------------------------------------- | -------------------------------------------------------- |
| Full-text search across parts, sets, minifigs, themes | Your user profile, sets, parts, and lists                |
| Part, set, minifig, color, theme, element lookup      | User collection mutations and list edits                 |
| Set and minifig inventories, recursive BOM expansion  | Live API v3 queries against current server data          |
| BOM normalization, diff, and validation               | API-only crosswalks and identifiers not in the downloads |
| CSV and BrickLink XML import/export                   |                                                          |
| Public page URL construction                          |                                                          |
| LDraw ↔ Rebrickable translation                       |                                                          |

## Features

- **Complete offline catalog.** All 12 official datasets — ~64k parts, ~28k
  sets, ~17k minifigs, ~114k elements, 275 colors, and 1.5M inventory rows —
  imported into a local SQLite database with full-text search.
- **Atomic, verified refresh.** Two-phase promotion flips a single active
  pointer only after schema, cross-file reference, FTS, row-count, and SQLite
  integrity checks pass. A failed refresh leaves the previous snapshot active.
- **Typed API v3 client.** 63 operations from the vendored OpenAPI document,
  covering parts, sets, minifigs, themes, colors, and user collections, with
  typed errors, pagination helpers, and header-only authentication.
- **Bills of materials.** Recursive expansion through contained sets and
  minifigs with per-row provenance, plus normalization (duplicate merging),
  diff, and validation against the catalog.
- **Import and export.** BrickLink XML, Rebrickable CSV, and JSON, with
  spreadsheet formula escaping on CSV output.
- **LDraw bridge.** Translate an LDraw model or BOM into Rebrickable parts and
  colors, with resolved/ambiguous/unresolved status per row.
- **Typed and async throughout.** Ships `py.typed`, every I/O path is `async`,
  and no catalog operation makes an implicit network call.

## CLI

Every command prints to stdout, keeps progress and diagnostics on stderr, and
returns a meaningful exit code (`0` ok, `2` invalid input, `3` missing data,
`4` incomplete translation, `5` API failure).

| Command                 | Purpose                                             |
| ----------------------- | --------------------------------------------------- |
| `status`                | Inspect and verify local catalog state              |
| `refresh`               | Explicitly refresh all catalog datasets (`--force`) |
| `search QUERY`          | Search the local catalog (`--kind`, `--limit`)      |
| `part PART_NUM`         | Show one part                                       |
| `set SET_NUM`           | Show a set, `--inventory`, or `--bom`               |
| `minifig FIG_NUM`       | Show a minifig, `--inventory`, or `--bom`           |
| `url KIND IDENTIFIER`   | Construct a public Rebrickable page URL             |
| `translate-ldraw MODEL` | Translate an LDraw model BOM (`--unresolved-only`)  |
| `api-spec`              | Print the vendored OpenAPI document                 |

`status`, `refresh`, `search`, `part`, `set`, `minifig`, and `translate-ldraw`
accept `--json`; BOM and translation output also accept `--csv`.

## Live API v3

Live calls are deliberately separate from the catalog and always take an
explicit key, authenticating only through the `Authorization` header:

```python
import asyncio
import os

from rebrickable import RebrickableClient


async def main() -> None:
    async with RebrickableClient(api_key=os.environ["REBRICKABLE_API_KEY"]) as client:
        part = await client.get_part("3001")
        print(part.name)
        async for lego_set in client.iter_sets(search="Galaxy Explorer"):
            print(lego_set.set_num, lego_set.name)


asyncio.run(main())
```

`Config.load()` reads YAML and then overlays `REBRICKABLE_API_KEY`. Ordinary
`Config.write()` calls omit the key; persisting it requires an explicit
opt-in. User tokens and passwords are never written to disk.

## Limitations

- Image URLs are preserved as metadata, but images are never downloaded,
  cached, rendered, or opened.
- API v3 exposes no general MOCs, MOC inventories, B-models, sub-sets, or
  pricing; alternate builds are the only MOC surface.
- Only the newest numeric inventory version is exposed publicly, and part
  relationships are reviewable candidates rather than proof of equivalence.
- Search ranking requires the runtime SQLite to be 3.35 or newer (March 2021);
  check `sqlite3.sqlite_version` if compatibility is uncertain.

The full list is in the [limitations reference](https://hbmartin.github.io/rebrickable/limitations/).

## Documentation

- [Quickstart](https://hbmartin.github.io/rebrickable/quickstart/)
- [CLI reference](https://hbmartin.github.io/rebrickable/cli/)
- [Data model](https://hbmartin.github.io/rebrickable/data-model/)
- [API client](https://hbmartin.github.io/rebrickable/api/)
- [LDraw bridge](https://hbmartin.github.io/rebrickable/ldraw-bridge/)
- [Agent recipes](https://hbmartin.github.io/rebrickable/agent-recipes/) — bounded-output patterns for LLM agents
- [TUI integration](https://hbmartin.github.io/rebrickable/tui-integration/) — read-only embedding guide
- [Limitations](https://hbmartin.github.io/rebrickable/limitations/)
- [Changelog](https://github.com/hbmartin/rebrickable/blob/main/CHANGELOG.md)

## Related projects

- [pyldraw3](https://github.com/hbmartin/pyldraw3) — modern Python package for
  creating and manipulating LDraw files, the CAD standard for LEGO models.
  Powers this project's optional `[ldraw]` extra.
- [pyldraw3-tui](https://github.com/hbmartin/pyldraw3-tui) — terminal UI for
  browsing the LDraw parts catalog and inspecting model files.
- [legolization](https://github.com/hbmartin/legolization) — turn a colored
  voxel model into a physically buildable LEGO model in LDraw format, with
  step-by-step instructions and a bill of materials.

## Development

```console
uv sync --all-groups
uv run ruff check src scripts tests
uv run ty check src/rebrickable
uv run pytest
```

Tests run with sockets disabled by default. Network-dependent suites are opt-in
through markers: `-m integration` uses current Rebrickable downloads or the
API, and `-m mutation` performs guarded user-account mutations. CI enforces
Ruff with `select = ["ALL"]`, two type checkers, and 97% branch coverage across
Python 3.12, 3.13, and 3.14.

## Status and license

Version 1.0.0 is a stable release with a committed public API. Breaking changes
will follow semantic versioning.

Distributed under GPL-3.0-or-later. See
[LICENSE.txt](https://github.com/hbmartin/rebrickable/blob/main/LICENSE.txt).

LEGO® is a trademark of the LEGO Group. Rebrickable is a trademark of
Rebrickable Pty Ltd. This project is not endorsed by either organization.
