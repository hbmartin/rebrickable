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

Minimum dependency versions for the reviewed upstream security fixes are
declared in the [package metadata](https://github.com/hbmartin/rebrickable/blob/main/pyproject.toml), including the LDraw extra.

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
- **Catalog analytics and history.** Query part usage, set occurrences,
  relationships, theme ancestry/descendants, all upstream inventory versions,
  and typed version-to-version inventory diffs without live API data.
- **Import and export.** BrickLink XML, Rebrickable CSV, and JSON, with
  spreadsheet formula escaping on CSV output.
- **LDraw bridge.** Translate an LDraw model or BOM into Rebrickable parts and
  colors, with resolved/ambiguous/unresolved status per row.
- **Typed and async-first.** Ships `py.typed`, exposes asynchronous catalog and
  API operations, and makes no implicit network calls from catalog queries.

## CLI

Every command prints to stdout, keeps progress and diagnostics on stderr, and
returns a meaningful exit code (`0` ok, `2` invalid input, `3` missing data,
`4` incomplete translation, `5` API failure).

When a stdout consumer closes early, the CLI exits quietly with `0`, preserving
any nonzero status already computed. A closed stderr drops further diagnostics
while the command continues with stdout and its own exit status.

| Command                 | Purpose                                             |
| ----------------------- | --------------------------------------------------- |
| `status`                | Inspect and verify local catalog state              |
| `refresh`               | Explicitly refresh all catalog datasets (`--force`) |
| `search QUERY`          | Search the local catalog (`--kind`, `--limit`)      |
| `part PART_NUM`         | Show a part, usage, set occurrences, or relationships |
| `set SET_NUM`           | Show a set, versioned inventory, or recursive BOM   |
| `minifig FIG_NUM`       | Show a minifig, `--inventory`, or `--bom`           |
| `catalog ...`           | Show the DB path, diagnose setup, or compare history |
| `bom ...`               | Normalize, diff, or validate BOM files              |
| `api ...`               | Read-only live API lookups using the configured key |
| `url KIND IDENTIFIER`   | Construct a public Rebrickable page URL             |
| `translate-ldraw MODEL` | Translate an LDraw model BOM (`--unresolved-only`)  |
| `api-spec`              | Print the vendored OpenAPI document                 |

Use global `--format table|json|csv|yaml`; legacy per-command `--json` and BOM or
translation `--csv` flags remain supported. `search` exposes all catalog
filters, including recursive subtheme matching and offsets.

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
- Part relationships are reviewable candidates rather than proof of physical
  equivalence. Historical inventory versions report upstream history, not
  manufacturing or market availability.
- Search ranking requires the runtime SQLite to be 3.35 or newer (March 2021);
  check `sqlite3.sqlite_version` if compatibility is uncertain.

The full list is in the [limitations reference](https://hbmartin.github.io/rebrickable/limitations/).

## Documentation

- [Quickstart](https://hbmartin.github.io/rebrickable/quickstart/)
- [Migrating from 1.0 to 1.1](https://hbmartin.github.io/rebrickable/migration-1.1/)
- [CLI reference](https://hbmartin.github.io/rebrickable/cli/)
- [Data model](https://hbmartin.github.io/rebrickable/data-model/)
- [API client](https://hbmartin.github.io/rebrickable/api/)
- [LDraw bridge](https://hbmartin.github.io/rebrickable/ldraw-bridge/)
- [Agent recipes](https://hbmartin.github.io/rebrickable/agent-recipes/) — bounded-output patterns for LLM agents
- [TUI integration](https://hbmartin.github.io/rebrickable/tui-integration/) — read-only embedding guide
- [Limitations](https://hbmartin.github.io/rebrickable/limitations/)
- [Issue tracker](https://github.com/hbmartin/rebrickable/issues)

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
uv sync --all-extras --all-groups --locked
uv run python scripts/validate.py static
uv run python scripts/validate.py types
uv run python scripts/validate.py tests
uv run python scripts/validate.py generated
uv run python scripts/validate.py performance
```

Tests run with sockets disabled by default. Network-dependent suites are opt-in
through markers: `-m integration` uses current Rebrickable downloads or the
API, and `-m mutation` performs guarded user-account mutations. CI enforces
Ruff with `select = ["ALL"]`, strict Pyrefly plus `ty` on source and consumer
examples, and independent coverage gates across Python 3.12, 3.13, and 3.14:
**97% combined statement/branch coverage** and **95% of branches**. The branch
checker compares raw counts without rounding and rejects incomplete reports.
Source tests use asyncio debugging, strict markers/configuration, strict expected
failures, warnings as errors, and explicitly exclude integration and mutations.

Hypothesis uses 100 reproducible examples by default; manual validation can use
1,000 with `uv run python scripts/validate.py tests --profile extended`. Synthetic
imports exercise 10,000 parts and 10,000/100,000 inventory rows, with a 32 MiB
Python allocation peak, 8 MiB growth limit, and 512 MiB process RSS ceiling on
Linux. Evidence-query tests span the 300-row batching boundary.

Workflow linting requires actionlint **1.7.12**, ShellCheck **0.11.0**, and zizmor
**1.30.1**: run `uv run python scripts/validate.py workflows`. Linux CI installs
these binaries with `scripts/install_workflow_tools.py`, checking pinned SHA-256
digests before extracting executables. On other platforms, install those same
versions locally. The one narrow zizmor exception retains GitHub's supported
relative syntax for same-commit reusable release validation.

CI tests installed wheels outside the checkout on Linux, macOS, and Windows,
with and without LDraw, and resolves Python 3.12 runtime lower bounds separately
from locked tooling. `uv build --clear` followed by
`uv run python scripts/check_distributions.py --mode rebuild` verifies metadata,
license, type marker, resources, dependency consistency, and an sdist-built wheel.
Use `--mode core`, `--mode ldraw`, or `--mode minimum` for isolated install checks.
Publication waits for validation of the exact artifacts it uploads.

Security, upstream drift, integration, and real-catalog benchmarks have no cron
triggers. Dependency auditing, Semgrep, and both CodeQL suites run independently
and retain diagnostics. Validation and security accept an optional full expected
commit SHA on manual dispatch; drift automation explicitly dispatches both after
pushing and fails visibly on command errors. Manual integration retains its
credential and disposable-account guards, with account mutations off by default.
The manual real-catalog benchmark records five warmed samples, timing medians,
snapshot fingerprints, and refresh/query RSS, enforcing a default 512 MiB ceiling.
Documentation deployment continues on pushes to `main`.

Cancellation drains active imports, bridge writes, and connection workers.
Once snapshot promotion starts, activation and pruning finish before cancellation
propagates; see the [quickstart](https://hbmartin.github.io/rebrickable/quickstart/) for the cancellation contract.
CodeQL centrally excludes the unused-import and ineffectual-statement queries;
cycle and unused-global checks remain enabled, with Ruff covering unused imports.

Package metadata must score 10/10 with
[pyroma621](https://pypi.org/project/pyroma621/), both from the checkout and the
built source distribution. The newer checker is pinned to 6.1.0 so upgrades
can be reviewed for changed checks or false positives. It provides the `pyroma`
command and Python module; install it instead of the original `pyroma`
distribution to avoid conflicts. To get machine-readable findings, run
`uv run --locked pyroma --format json --min 10 .`.
Advisory findings appear in the output; add `--strict` to include them in the
score.

The separate [garuff](https://github.com/gahjelle/garuff) workflow is advisory
while this experimental checker is evaluated. It runs on Python 3.14 in an
isolated tool environment, pinned to a reviewed Git revision, and uploads its
output and exit code as the `garuff-report` artifact. Findings and tool failures
do not block the existing quality checks. Run the same check locally with:

```console
uv tool run --python 3.14 --from "garuff @ git+https://github.com/gahjelle/garuff@7d126c00f13b4d94f39f1107934089241ab7a6a1" garuff check src scripts tests docs
```

Exit `0` means clean, `1` includes findings or parse/suppression errors, and `2`
includes configuration or command errors. Installation failures and crashes
also need review. Project-specific rule exclusions and evaluation notes are in
[the agent recipes](https://hbmartin.github.io/rebrickable/agent-recipes/#experimental-garuff-checks). Review
the upstream rule changes before updating the pinned revision in both places;
do not apply automatic fixes across public APIs or generated files.

Contributions should include focused tests, pass the commands above, preserve
the async-only API, and avoid network access in unit tests. Open an issue before
large compatibility or data-model changes. Report vulnerabilities privately
through GitHub's **Security → Report a vulnerability** flow; do not open a
public issue for an undisclosed security problem. Supported releases receive
security fixes on the latest published version.

If migrating from the unrelated `pyrebrickable` distribution, uninstall it
before installing this package: the projects have different imports, models,
and compatibility guarantees. `rebrickable catalog doctor` detects a concurrent
`pyrebrickable` installation and reports the active catalog and runtime details.

## Status and license

The package is in beta while its first public release receives wider integration
testing. Breaking changes follow semantic versioning.

Distributed under GPL-3.0-or-later. See
[LICENSE.txt](https://github.com/hbmartin/rebrickable/blob/main/LICENSE.txt).

LEGO® is a trademark of the LEGO Group. Rebrickable is a trademark of
Rebrickable Pty Ltd. This project is not endorsed by either organization.
