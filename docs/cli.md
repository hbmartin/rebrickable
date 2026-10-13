# CLI reference

The CLI is non-interactive and does not expose user mutations or images. Live
API commands are read-only:

```text
rebrickable status [--json]
rebrickable refresh [--force] [--json]
rebrickable [--format table|json|csv|yaml] search [QUERY] [FILTERS] [--limit N] [--offset N]
rebrickable part PART_NUM [--usage [--color-id ID] | --sets [--color-id ID] [--include-spares] [--limit N] [--offset N] | --relationships]
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
translation, and `5` API/authentication/throttling failure. Flags a `part` mode
does not use (for example `--limit` outside `--sets`) and `--include-subthemes`
without `--theme-id` are rejected with the invalid-input status rather than
silently ignored. The global `--version` prints the package version; the
`set`/`minifig` `--version N` option instead selects an inventory version.

Piping output to a consumer that closes early (such as `head`) exits quietly
with `0`, unless the command has already computed a nonzero status. This also
applies to help and version output. A closed stderr drops further progress and
diagnostics without interrupting the command or discarding healthy stdout.
Usage errors retain status `2`, even when their diagnostics cannot be delivered.
Other errors writing or flushing command output, such as a full disk or an
encoding failure, return
`1` regardless of the selected command status. Help and version retain their
parser status `0` when output fails. Encoding failures preserve and flush
previously encoded output. Embedded calls to `cli.main()` never close or
redirect any host stream, including the original stdout/stderr. The installed
executable and `python -m rebrickable.cli` handle shutdown cleanup for failed
original process streams. Embedded hosts must handle failed borrowed streams
before interpreter shutdown; the returned status cannot prevent a later Python
flush error from overriding the process exit status. Use either executable
form for standalone execution. Diagnostic encoding failures do not suppress
encodable stdout when both roles share a stream. A Windows EINVAL is treated as a pipe interruption
only when the stream's descriptor identifies a pipe. Diagnostic-only failures
preserve the command status even with aliased stdout/stderr. Shared streams
are finalized once; a new shared flush failure counts as a stdout failure only
when the invocation produced stdout. Each operational stdout failure is
reported once.

Search accepts year, theme, recursive-subtheme (requires `--theme-id`),
part-count, category, material, limit, and offset filters. `catalog doctor`
reports Python/SQLite/package versions, catalog diagnostics, API-key presence
without revealing the key, and conflicting `pyrebrickable` installations.
`catalog versions` exits with missing-data status for an unknown owner. Live
`api` commands require `REBRICKABLE_API_KEY` or `api_key` in configuration and
never mutate a user account.

`translate-ldraw` requires `rebrickable[ldraw]`. When that extra is missing, the
command prints an installation diagnostic and exits with invalid-input status.
Pass `--ldraw-library` pointing at an LDraw `parts.lst` so color codes can be
matched by RGB and name; without it, bare color codes resolve only through
overrides and cached crosswalks. Loader diagnostics (for example an incomplete
model source) are printed to stderr prefixed with `[ldraw]`. `--unresolved-only`
selects every row that is not fully resolved, including ambiguous rows, in both
JSON and CSV output.
