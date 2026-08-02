# CLI reference

The initial CLI is intentionally flat, non-interactive, read-only, and does not
expose user mutations or images:

```text
rebrickable status [--json]
rebrickable refresh [--force] [--json]
rebrickable search QUERY [--kind KIND] [--limit N] [--json]
rebrickable part PART_NUM [--json]
rebrickable set SET_NUM [--inventory | --bom] [--include-spares] [--json|--csv]
rebrickable minifig FIG_NUM [--inventory | --bom] [--include-spares] [--json|--csv]
rebrickable url {part|set|minifig} ID
rebrickable translate-ldraw MODEL [--json|--csv] [--unresolved-only]
rebrickable api-spec [--output PATH]
```

Machine JSON is schema-versioned and deterministic; CSV uses RFC 4180 line
endings. Machine output alone goes to stdout. Refresh progress and diagnostics go
to stderr. Exit statuses are fixed: `0` success, `1` unexpected failure, `2`
usage or invalid input, `3` missing catalog/entity/data, `4` incomplete
translation, and `5` API/authentication/throttling failure.

`translate-ldraw` requires `rebrickable[ldraw]`. When that extra is missing, the
command prints an installation diagnostic and exits with invalid-input status.
