# CLI reference

The CLI is non-interactive and does not expose user mutations or images. Live
API commands are read-only:

```text
rebrickable status [--json]
rebrickable refresh [--force] [--json]
rebrickable [--format table|json|csv|yaml] search [QUERY] [FILTERS] [--offset N]
rebrickable part PART_NUM [--usage | --sets | --relationships] [--color-id ID]
rebrickable set SET_NUM [--inventory | --bom] [--version N] [--include-spares]
rebrickable minifig FIG_NUM [--inventory | --bom] [--version N] [--include-spares]
rebrickable catalog {path|doctor|versions|diff} ...
rebrickable bom {normalize|validate|diff} ...
rebrickable api {part|set|minifig|parts|sets|minifigs} ...
rebrickable url {part|set|minifig} ID
rebrickable translate-ldraw MODEL [--json|--csv] [--unresolved-only] [--ldraw-library PARTS_LST]
rebrickable api-spec [--output PATH]
```

Machine JSON is schema-versioned and deterministic; CSV uses RFC 4180 line
endings. Machine output alone goes to stdout. Refresh progress and diagnostics go
to stderr. Exit statuses are fixed: `0` success, `1` unexpected failure, `2`
usage or invalid input, `3` missing catalog/entity/data, `4` incomplete
translation, and `5` API/authentication/throttling failure.

Search accepts year, theme, recursive-subtheme, part-count, category, material,
limit, and offset filters. `catalog doctor` reports Python/SQLite/package
versions, catalog diagnostics, API-key presence without revealing the key, and
conflicting `pyrebrickable` installations. Live `api` commands require
`REBRICKABLE_API_KEY` or `api_key` in configuration and never mutate a user
account.

`translate-ldraw` requires `rebrickable[ldraw]`. When that extra is missing, the
command prints an installation diagnostic and exits with invalid-input status.
Pass `--ldraw-library` pointing at an LDraw `parts.lst` so color codes can be
matched by RGB and name; without it, bare color codes resolve only through
overrides and cached crosswalks. Loader diagnostics (for example an incomplete
model source) are printed to stderr prefixed with `[ldraw]`. `--unresolved-only`
selects every row that is not fully resolved, including ambiguous rows, in both
JSON and CSV output.
