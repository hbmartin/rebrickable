# AGENTS.md

This file gives coding agents the repository-specific context and working rules
needed to make safe, reviewable changes to `rebrickable`.

## Project overview

`rebrickable` is a typed, asynchronous Python package and scriptable CLI for
the official Rebrickable catalog, API v3, inventories, bills of materials, and
optional LDraw cross-referencing.

The package has two deliberately separate data paths:

- The downloaded catalog is offline-first. An explicit refresh imports the
  official CSV datasets into an immutable SQLite snapshot. Ordinary catalog
  queries must not perform network I/O.
- `RebrickableClient` accesses the live Rebrickable API and requires an
  explicit API key. Account mutations are guarded and must never happen as an
  incidental consequence of catalog or CLI work.

Preserve these project constraints unless the user explicitly changes them:

- The public Python API is asynchronous; do not add a synchronous facade.
- Python 3.12 is the minimum supported version. Do not add Python 3.11 support
  or syntax workarounds for it.
- The project is beta and follows semantic versioning.
- The license is GPL-3.0-or-later.
- The current SQLite schema is version 1. Existing compatible snapshots should
  remain readable unless a task explicitly includes a schema migration plan.

Read `README.md`, the relevant page under `docs/`, and `pyproject.toml` before
changing public behavior. For compatibility-sensitive work, also read
`docs/migration-1.1.md`.

## Development commands

Run commands from the repository root. The project uses `uv` for dependency
management, environments, running tools, and packaging. Using `uv run` means
manual virtual-environment activation is not required.

### Setup

```bash
uv sync --all-extras --all-groups --locked
```

Drop `--locked` only when intentionally changing project metadata or
dependencies, and review `uv.lock` for registry or unrelated dependency churn.

### Fast verification

```bash
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run ty check src/rebrickable
uv run pyrefly check src/rebrickable
uv run pytest -q
```

### Full local verification

```bash
uv run ruff format src scripts tests
uv run ruff check src scripts tests
uv run ty check src/rebrickable
uv run pyrefly check src/rebrickable
uv run deptry .
uv run pyroma --min 10 .
uv run zensical build --clean --strict
uv run pytest --cov=rebrickable --cov-branch --cov-report=term -q
uv build --clear
```

The enforced branch-coverage floor is 97%. Do not lower it to make a change
pass. CI runs the quality suite on Python 3.12, 3.13, and 3.14 and runs package
smoke tests on Linux, macOS, and Windows.

### Integration and mutation tests

Unit tests run with sockets disabled. Networked tests are opt-in:

```bash
uv run python -m pytest tests/integration/test_live_readonly.py -m integration
uv run python -m pytest tests/integration/test_mutation_guarded.py -m mutation
```

The read-only suite uses current downloads or live API responses and may need
credentials. Never run the mutation suite without explicit user authorization,
the intended account credentials, and an understanding of the guard variables
in the test module.

### Documentation and artifacts

```bash
uv run zensical build --clean --strict
uv build --clear
uv run rebrickable --version
```

Wheels should contain the importable package, `py.typed`, and vendored OpenAPI
data. Source distributions should additionally contain docs, scripts, and
tests. The package version is declared in `pyproject.toml` and mirrored in the
root package entry in `uv.lock`.

### Catalog benchmark

After a catalog has been refreshed locally:

```bash
uv run python scripts/benchmark_catalog.py
```

Treat the benchmark as a regression gate, not a microbenchmark competition.
Preserve result correctness and bounded memory use before optimizing timings.

## Architecture

### Offline catalog

- `src/rebrickable/session.py` is the composition root. It owns the pinned
  SQLite connection and exposes repository facades, search, validation, and
  optional LDraw integration. `open()` is lazy; `connect()` eagerly verifies
  and opens the catalog.
- `src/rebrickable/refresh.py` downloads, validates, imports, and atomically
  promotes snapshots. A failed refresh must leave the previous active snapshot
  usable.
- `src/rebrickable/catalog/database.py` resolves and verifies snapshot state.
- `src/rebrickable/catalog/importers.py` imports the official CSV datasets.
- `src/rebrickable/catalog/schema.py` owns `SCHEMA_VERSION` and the SQLite
  schema.
- `src/rebrickable/catalog/queries.py` contains session-bound repositories and
  analytical queries.
- `src/rebrickable/catalog/inventory.py` loads inventories and expands recursive
  bills of materials.
- `src/rebrickable/catalog/search.py` implements bounded full-text search and
  filtering.
- `src/rebrickable/catalog/models.py` contains stable offline domain models.

Do not create a runtime import from catalog leaf modules back to `session.py`.
Use a small structural `Protocol` when a repository only needs part of the
session interface.

### Live API

- `src/rebrickable/api/client.py` contains the async public client, wrappers,
  pagination helpers, and guarded mutations.
- `src/rebrickable/api/transport.py` owns HTTP transport behavior.
- `src/rebrickable/api/models.py` contains immutable Pydantic request and
  response DTOs. Known stable response fields should be named; unknown upstream
  fields remain preserved in `extra`.
- `src/rebrickable/api/operation_registry.py` and
  `src/rebrickable/api/query_types.py` are generated from the vendored Swagger
  document plus the reviewed compatibility overlay in
  `scripts/generate_openapi.py`.
- `src/rebrickable/data/` contains the dated, vendored OpenAPI input used for
  deterministic generation.

Do not hand-edit generated registry or query-type output. For the existing
snapshot, regeneration must pass its checksum gate and produce byte-identical
files. For a deliberately accepted upstream snapshot, use
`scripts/update_openapi.py`, review the operation count and complete diff, and
update client models, wrappers, docs, and tests as needed. Do not use
`--allow-new-checksum` merely to silence drift.

### BOM, exports, LDraw, and CLI

- `src/rebrickable/bom.py` normalizes, diffs, and validates bills of materials.
- `src/rebrickable/exports.py` handles JSON, CSV, and BrickLink XML. Preserve
  spreadsheet-formula escaping and bounded parsing behavior.
- `src/rebrickable/bridge/` provides optional pyldraw3 translation. The core
  package must continue to import and work without the `ldraw` extra installed.
- `src/rebrickable/cli.py` is the argparse CLI. Structured data goes to stdout;
  diagnostics and progress go to stderr. Preserve documented exit statuses.
  Global `--format` appears before the subcommand; legacy local JSON/CSV flags
  remain compatibility surfaces.
- `src/rebrickable/config.py` and `src/rebrickable/dirs.py` own configuration
  loading, secret persistence rules, and platform-specific paths.
- `src/rebrickable/types.py`, `src/rebrickable/errors.py`, and top-level
  `src/rebrickable/__init__.py` define shared contracts and public re-exports.

## Data, network, and security rules

- Never introduce implicit network access into catalog reads, search, BOM
  operations, imports/exports, or normal session startup.
- Unit tests must be deterministic and pass with sockets disabled. Use mocked
  transports or local fixtures instead of live services.
- Never log, serialize into errors, or persist an API key by default.
  Configuration writes require an explicit opt-in before including secrets.
- Never perform user-account mutations unless the task explicitly requests
  them. Preserve confirmation guards on replacement or destructive operations.
- Use parameterized SQLite statements for values. If SQL identifiers or clauses
  must be dynamic, select them from a closed internal allowlist.
- Treat downloaded CSV, XML, YAML, JSON, LDraw, and API payloads as untrusted
  input. Preserve size/depth/page bounds and safe XML/YAML parsing.
- Do not modify an active catalog database in place. Refresh work belongs in a
  staging snapshot that is validated before atomic promotion.
- Relationship data represents reviewable candidates, not guaranteed physical
  equivalence. Inventory history is catalog revision history, not market or
  manufacturing history.

## Testing practices

- Add focused regression tests for changed behavior and failure paths.
- Keep ordinary tests independent of a user's real config, credentials,
  application-data directories, and catalog snapshot.
- Put reusable local catalog fixtures in `tests/conftest.py`; keep live cases in
  `tests/integration/` under the correct marker.
- For async APIs, test cancellation, cleanup, pagination/bounds, and partial or
  malformed upstream responses where relevant.
- For CLI changes, test stdout, stderr, serialization format, and exit status.
- For repository or query changes, test missing entities, empty results,
  ordering, limits/offsets, historical versions, and snapshot pinning as
  applicable.
- For public models, test parsing, immutability, serialization, unknown-field
  preservation, and promoted typed fields.
- Avoid assertions tied to current upstream row counts or network content in
  unit tests.

## Python practices

- Add precise type hints and keep both `ty` and Pyrefly clean. Do not solve
  import cycles with runtime imports used only for typing; prefer postponed
  annotations, `TYPE_CHECKING`, or structural protocols as appropriate.
- Use Python 3.12 syntax: built-in generics (`list`, `dict`, `tuple`), `|` for
  unions, and `Self` where it accurately describes the return type.
- Prefer frozen, slotted dataclasses for stable internal/domain values and
  frozen Pydantic models for validated API DTOs.
- Use `pathlib.Path` for filesystem paths rather than `os.path`.
- Prefer f-strings over formatting or concatenation, except for lazy logging
  arguments.
- Use named arguments when they materially improve clarity, especially for
  calls with several same-typed parameters.
- Use comprehensions, assignment expressions, async iterators, and structural
  pattern matching when they make the code clearer; do not force them into
  simple code.
- Keep public iteration and query APIs bounded or paginated. Avoid materializing
  a complete catalog table unless the API explicitly requires it.
- Raise the package's structured exceptions with safe, actionable details.
  Never place credentials or full sensitive payloads in exception text.

## Public API, documentation, and compatibility

- Public names are re-exported deliberately. When adding one, update the
  relevant package `__init__.py`, `__all__`, type checking, docs, and tests.
- Preserve async signatures, keyword names, return-model semantics, and CLI
  compatibility unless a breaking change is explicitly requested and
  documented.
- Additive Pydantic fields can change exact `model_dump()` shapes; update
  serialization tests and migration notes when promoting fields from `extra`.
- A schema change requires an explicit compatibility decision, a
  `SCHEMA_VERSION` update, refresh/verification changes, and documentation for
  existing snapshots. Do not silently make version-1 catalogs unreadable.
- Update the relevant `README.md` and existing `docs/` pages for user-visible
  behavior. Significant migration-sensitive changes belong in the applicable
  migration guide.
- Keep documentation examples executable and asynchronous. Clearly distinguish
  offline catalog examples from live API examples.

## Working practices

- Inspect the relevant implementation, tests, generated-file boundaries, and
  current Git status before editing. Use `rg`/`rg --files` for discovery.
- Preserve user changes and unrelated untracked files. Stage only files that
  belong to the requested change.
- Keep diffs focused. Avoid opportunistic dependency updates, lockfile churn,
  generated rewrites, or broad formatting outside the task.
- Do not add a dependency when the standard library or an existing dependency
  provides a clear solution.
- Do not commit, push, publish, mutate external accounts, or open/update a pull
  request unless the user asks for that action.
- Before handoff, report the exact checks run, any skipped integration coverage,
  and any remaining untracked or unrelated files.
