# Changelog

## Unreleased

Pre-release review fixes; several behaviors changed while the API is unreleased:

- `refresh()` re-imports after a schema upgrade instead of reporting UNCHANGED
  against an incompatible snapshot; promotion is two-phase and atomic, and
  superseded snapshots are pruned (new `Config.snapshot_retention`, default 1).
- Integrity checks (`PRAGMA quick_check`) run at catalog open time, not on
  every part/color resolution; `catalog_state`/`session.state` verify only when
  `verify=True` (the CLI `status` command always verifies).
- `bill_of_materials` returns `CatalogBom` with `skipped` diagnostics: contained
  sets/minifigs without inventories no longer abort expansion (`strict=True`
  restores the raise).
- Model translation resolves colors via pyldraw3 color metadata
  (`translate_model_path` gains `parts=`/`library_path=`/`tolerant=`; the CLI
  gains `--ldraw-library`), surfaces loader diagnostics, and classifies rows by
  the worst match status. `--unresolved-only` JSON now includes ambiguous rows.
- Crosswalk enrichment replaces stale cached mappings; multi-row cache hits and
  multiple overrides surface as AMBIGUOUS; reverse part resolution no longer
  fabricates confident matches from identifier equality alone.
- `PartRef` normalizes LDraw values; `ColorRef` coerces numeric strings; BOM
  merge/diff keys are LDraw-normalized; CSV exports escape spreadsheet formula
  prefixes; `to_json` serializes frozen mappings.
- API client: `sync_user_sets` and multi-item `add_user_sets` POST a JSON array
  body; `set_user_set_quantity` takes `QuantityRequest`;
  `replace_user_set_list_set`/`update_user_set_list_set` take
  `SetListSetUpdateRequest`; batch helpers raise `BatchMutationError` with
  partial progress; 3xx responses raise `ApiError`; non-JSON success bodies
  raise `ApiDecodeError`; server `Retry-After` beyond `Config.max_retry_after`
  (300 s default) raises instead of blocking; redaction covers per-call tokens.
- `rebrickable.api.generated_requests` was removed; the operation registry is
  the single request contract, enforced by a per-operation wire-contract test.
- Dependencies are locked against pypi.org; pyldraw3 resolves from PyPI 1.5.0.

## 1.0.0 — 2026-08-01

- First stable `rebrickable` release.
- Added transactional ingestion and offline querying for all 12 catalog datasets.
- Added explicit async coverage of all 63 vendored API v3 operations.
- Added recursive inventories, BOM validation/diffs/exports, unified search, and
  availability/substitution evidence.
- Added bidirectional LDraw mapping, optional public `pyldraw3` adapters, and
  ambiguity-preserving translation reports.
- Added the flat read-only CLI, consumer documentation, 97% branch coverage gate,
  and scheduled integration/OpenAPI-drift automation.
