"""Non-interactive command-line facade."""

from __future__ import annotations

import argparse
import asyncio
import errno
import io
import json
import os
import platform
import sqlite3
import stat
import sys
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from typing import IO, Any, Never, Protocol, override

import yaml

from rebrickable.api import RebrickableClient
from rebrickable.bom import Bom
from rebrickable.config import Config
from rebrickable.data import OPENAPI_RESOURCE
from rebrickable.errors import (
    ApiError,
    CatalogUnavailableError,
    EntityNotFoundError,
    OptionalDependencyError,
    RebrickableError,
)
from rebrickable.exports import (
    catalog_bom_to_csv,
    to_csv,
    to_json,
    translation_table,
    translation_to_csv,
)
from rebrickable.progress import ProgressEvent
from rebrickable.session import RebrickableSession
from rebrickable.types import (
    CatalogStatus,
    ColorSystem,
    ExitCode,
    MappingStatus,
    PartSystem,
    SearchFilters,
    SearchKind,
)
from rebrickable.urls import minifig_url, part_url, set_url


class _StdoutPipeClosedError(Exception):
    """Stop producing output after its consumer closes the pipe."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(status)


@dataclass(slots=True, kw_only=True)
class _OutputState:
    failed: list[tuple[IO[str], BaseException, bool]] = field(default_factory=list)
    write_failed: list[tuple[IO[str], str, BaseException]] = field(default_factory=list)
    owned: tuple[tuple[IO[str], int], ...] = ()
    stdout_written: list[IO[str]] = field(default_factory=list)
    finalized: list[IO[str]] = field(default_factory=list)


_output_state: ContextVar[_OutputState | None] = ContextVar("cli_output", default=None)


def _stream_failure(
    stream: IO[str], *, role: str, writing: bool = False
) -> tuple[BaseException, bool] | None:
    state = _output_state.get()
    if state is not None:
        for item, error, pipe in state.failed:
            if stream is item:
                return error, pipe
        if writing:
            for item, failed_role, error in state.write_failed:
                if stream is item and role == failed_role:
                    return error, False
    return None


def _is_broken_pipe(error: OSError, stream: IO[str]) -> bool:
    if (
        isinstance(error, BrokenPipeError)
        or error.errno == errno.EPIPE
        or getattr(error, "winerror", None) in {109, 232, 233}
    ):
        return True
    if os.name == "nt" and error.errno == errno.EINVAL:
        with suppress(AttributeError, OSError, ValueError):
            return stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
    return False


def _silence_stream(stream: IO[str]) -> None:
    """Prevent a failed stream from flushing again during interpreter shutdown."""
    state = _output_state.get()
    if state is None:
        return
    expected = next(
        (descriptor for original, descriptor in state.owned if stream is original),
        None,
    )
    if expected is None:
        return
    try:
        descriptor = stream.fileno()
        if descriptor != expected:
            return
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, descriptor)
        finally:
            if devnull != descriptor:
                with suppress(OSError):
                    os.close(devnull)
    except (AttributeError, OSError, ValueError):
        # EMFILE and replaced streams can prevent descriptor redirection.
        # Closing the raw layer discards buffered data without another flush.
        with suppress(AttributeError, OSError, ValueError):
            if isinstance(stream, io.TextIOWrapper):
                getattr(stream.buffer, "raw", stream.buffer).close()


def _fail_stream(
    stream: IO[str], error: BaseException, *, role: str, pipe: bool = False
) -> None:
    unusable = not isinstance(error, UnicodeError)
    state = _output_state.get()
    if state is not None:
        if unusable:
            state.failed.append((stream, error, pipe))
        else:
            state.write_failed.append((stream, role, error))
    if unusable:
        _silence_stream(stream)


def _write_stdout(value: str | bytes, *, status: int = ExitCode.OK) -> None:
    stream = sys.stdout
    if stream is None:
        raise _StdoutPipeClosedError(status)
    if failure := _stream_failure(stream, role="stdout", writing=True):
        error, pipe = failure
        if pipe:
            raise _StdoutPipeClosedError(status)
        raise OSError(f"unable to write stdout: {error}") from error
    try:
        if stream.closed:
            raise _StdoutPipeClosedError(status)
        if isinstance(value, bytes):
            stream.buffer.write(value)
        else:
            stream.write(value)
        state = _output_state.get()
        if state is not None and value:
            state.stdout_written.append(stream)
    except (AttributeError, OSError, ValueError) as exc:
        pipe_closed = isinstance(exc, OSError) and _is_broken_pipe(exc, stream)
        _fail_stream(stream, exc, role="stdout", pipe=pipe_closed)
        if isinstance(exc, OSError):
            if pipe_closed:
                raise _StdoutPipeClosedError(status) from None
            raise
        raise OSError(f"unable to write stdout: {exc}") from exc


def _print_stdout(value: object, *, status: int = ExitCode.OK) -> None:
    _write_stdout(f"{value}\n", status=status)


def _write_stderr(message: str) -> None:
    stream = sys.stderr
    if stream is None:
        return
    try:
        if stream.closed or _stream_failure(stream, role="stderr", writing=True):
            return
        stream.write(message)
    except (AttributeError, OSError, ValueError) as exc:
        _fail_stream(
            stream,
            exc,
            role="stderr",
            pipe=isinstance(exc, OSError) and _is_broken_pipe(exc, stream),
        )


def _print_stderr(message: str) -> None:
    _write_stderr(f"{message}\n")


def _flush_stream(
    stream: IO[str] | None,
    *,
    role: str,
) -> AttributeError | OSError | ValueError | None:
    if stream is None:
        return None
    state = _output_state.get()
    if state is not None:
        if any(stream is item for item in state.finalized):
            return None
        state.finalized.append(stream)
        # A write already established the physical failure. Reclassifying it
        # here would duplicate diagnostics or blame stderr failures on stdout.
        if _stream_failure(stream, role=role):
            return None
        if stream is sys.stderr and stream is sys.stdout:
            role = (
                "stdout"
                if any(stream is item for item in state.stdout_written)
                else "stderr"
            )
    try:
        if stream.closed:
            return None
        stream.flush()
    except (AttributeError, OSError, ValueError) as exc:
        pipe_closed = isinstance(exc, OSError) and _is_broken_pipe(exc, stream)
        _fail_stream(stream, exc, role=role, pipe=pipe_closed)
        if not pipe_closed and role == "stdout":
            return exc
    return None


class _MessageWriter(Protocol):
    def write(self, value: str, /) -> object:
        """Write an argparse message to its selected output stream."""


class _ArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        self._print_message(self.format_usage(), sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")

    @override
    def _print_message(self, message: str, file: _MessageWriter | None = None) -> None:
        if message:
            with suppress(AttributeError, OSError, ValueError, _StdoutPipeClosedError):
                if file is sys.stdout:
                    _write_stdout(message)
                elif file is None or file is sys.stderr:
                    _write_stderr(message)
                else:
                    file.write(message)

    @override
    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if message:
            self._print_message(message, sys.stderr)
        raise SystemExit(status)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="rebrickable")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {metadata.version('rebrickable')}",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json", "csv", "yaml"),
        default="table",
        help="default output format (legacy per-command --json/--csv flags remain supported)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="inspect local catalog state")
    status.add_argument("--json", action="store_true")

    refresh = sub.add_parser("refresh", help="explicitly refresh all catalog datasets")
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="search the local catalog")
    search.add_argument("query", nargs="?", default="")
    search.add_argument(
        "--kind", action="append", choices=[kind.value for kind in SearchKind]
    )
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--year-from", type=int)
    search.add_argument("--year-to", type=int)
    search.add_argument("--theme-id", type=int)
    search.add_argument("--include-subthemes", action="store_true")
    search.add_argument("--min-parts", type=int)
    search.add_argument("--max-parts", type=int)
    search.add_argument("--category-id", type=int)
    search.add_argument("--material")
    search.add_argument("--json", action="store_true")

    part = sub.add_parser("part", help="show one part")
    part.add_argument("part_num")
    part_mode = part.add_mutually_exclusive_group()
    part_mode.add_argument("--usage", action="store_true")
    part_mode.add_argument("--sets", action="store_true")
    part_mode.add_argument("--relationships", action="store_true")
    part.add_argument("--color-id", type=int)
    part.add_argument("--include-spares", action="store_true")
    part.add_argument("--limit", type=int, default=None)
    part.add_argument("--offset", type=int, default=None)
    part.add_argument("--json", action="store_true")

    for name, identifier in (("set", "set_num"), ("minifig", "fig_num")):
        entity = sub.add_parser(name, help=f"show one {name}")
        entity.add_argument(identifier)
        mode = entity.add_mutually_exclusive_group()
        mode.add_argument("--inventory", action="store_true")
        mode.add_argument("--bom", action="store_true")
        entity.add_argument("--include-spares", action="store_true")
        entity.add_argument("--version", type=int, help="select an inventory version")
        output = entity.add_mutually_exclusive_group()
        output.add_argument("--json", action="store_true")
        output.add_argument("--csv", action="store_true")

    url = sub.add_parser("url", help="construct a public entity page URL")
    url.add_argument("kind", choices=("part", "set", "minifig"))
    url.add_argument("identifier")

    translate = sub.add_parser("translate-ldraw", help="translate an LDraw model BOM")
    translate.add_argument("model", type=Path)
    output = translate.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--csv", action="store_true")
    translate.add_argument("--unresolved-only", action="store_true")
    translate.add_argument(
        "--ldraw-library",
        type=Path,
        help="path to an LDraw parts.lst providing color metadata",
    )

    spec = sub.add_parser("api-spec", help="print the vendored OpenAPI document")
    spec.add_argument("--output", type=Path)

    catalog = sub.add_parser("catalog", help="inspect catalog storage and history")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("path", help="print the active snapshot database path")
    catalog_sub.add_parser(
        "doctor", help="check runtime, configuration, and package conflicts"
    )
    versions = catalog_sub.add_parser("versions", help="list inventory versions")
    versions.add_argument("owner_num")
    inventory_diff = catalog_sub.add_parser(
        "diff", help="compare two inventory versions"
    )
    inventory_diff.add_argument("owner_num")
    inventory_diff.add_argument("before_version", type=int)
    inventory_diff.add_argument("after_version", type=int)

    bom = sub.add_parser("bom", help="normalize, diff, or validate a BOM file")
    bom_sub = bom.add_subparsers(dest="bom_command", required=True)
    for command in ("normalize", "validate"):
        item = bom_sub.add_parser(command)
        item.add_argument("input", type=Path)
        _add_bom_input_options(item)
    bom_diff = bom_sub.add_parser("diff")
    bom_diff.add_argument("before", type=Path)
    bom_diff.add_argument("after", type=Path)
    _add_bom_input_options(bom_diff)

    api = sub.add_parser("api", help="perform read-only live API lookups")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    for name, identifier in (
        ("part", "part_num"),
        ("set", "set_num"),
        ("minifig", "fig_num"),
    ):
        item = api_sub.add_parser(name)
        item.add_argument(identifier)
    for name in ("parts", "sets", "minifigs"):
        item = api_sub.add_parser(name)
        item.add_argument("--search")
        item.add_argument("--page", type=int)
        item.add_argument("--page-size", type=int, default=100)
    return parser


def _add_bom_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input-format",
        choices=("csv", "rebrickable-csv", "bricklink-xml"),
        default="csv",
    )
    parser.add_argument(
        "--part-system",
        choices=[item.value for item in PartSystem],
        default=PartSystem.LDRAW.value,
    )
    parser.add_argument(
        "--color-system",
        choices=[item.value for item in ColorSystem],
        default=ColorSystem.LDRAW.value,
    )


_PART_MODE_FLAGS: dict[str | None, frozenset[str]] = {
    None: frozenset(),
    "usage": frozenset({"--color-id"}),
    "relationships": frozenset(),
    "sets": frozenset({"--color-id", "--include-spares", "--limit", "--offset"}),
}


def _check_part_flags(args: argparse.Namespace) -> None:
    mode = next(
        (name for name in ("usage", "sets", "relationships") if getattr(args, name)),
        None,
    )
    provided = {
        "--color-id": args.color_id is not None,
        "--include-spares": args.include_spares,
        "--limit": args.limit is not None,
        "--offset": args.offset is not None,
    }
    allowed = _PART_MODE_FLAGS[mode]
    rejected = [
        flag for flag, given in provided.items() if given and flag not in allowed
    ]
    if rejected:
        target = f"part --{mode}" if mode else "a plain part lookup"
        raise ValueError(f"{', '.join(rejected)} not supported with {target}")


def _print_entity(
    value: Any,
    *,
    json_output: bool = False,
    output_format: str = "table",
    status: int = ExitCode.OK,
) -> None:
    if json_output or output_format == "json":
        _write_stdout(to_json(value, schema="rebrickable.entity"), status=status)
    elif output_format == "yaml":
        data = json.loads(to_json(value, schema="rebrickable.entity"))["data"]
        _write_stdout(yaml.safe_dump(data, sort_keys=True), status=status)
    elif output_format == "csv":
        _write_stdout(to_csv(value), status=status)
    else:
        _print_stdout(value, status=status)


def _read_bom(path: Path, args: argparse.Namespace) -> Bom:
    if args.input_format == "bricklink-xml":
        return Bom.from_bricklink_xml(path)
    if args.input_format == "rebrickable-csv":
        return Bom.from_rebrickable_csv(path)
    return Bom.from_csv(
        path,
        part_system=PartSystem(args.part_system),
        color_system=ColorSystem(args.color_system),
    )


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


async def _api_command(config: Config, args: argparse.Namespace) -> int:
    if not config.api_key:
        raise ValueError(
            "live API commands require REBRICKABLE_API_KEY or api_key in config"
        )
    async with RebrickableClient(api_key=config.api_key, config=config) as client:
        if args.api_command == "part":
            value = await client.get_part(args.part_num)
        elif args.api_command == "set":
            value = await client.get_set(args.set_num)
        elif args.api_command == "minifig":
            value = await client.get_minifig(args.fig_num)
        else:
            query = {
                "search": args.search,
                "page": args.page,
                "page_size": args.page_size,
            }
            if args.api_command == "parts":
                value = await client.list_parts(**query)
            elif args.api_command == "sets":
                value = await client.list_sets(**query)
            else:
                value = await client.list_minifigs(**query)
    _print_entity(value, output_format=args.output_format)
    return ExitCode.OK


def _progress(event: ProgressEvent) -> None:
    detail = f" {event.dataset}" if event.dataset else ""
    count = f" {event.current}" if event.current is not None else ""
    _print_stderr(
        f"[{event.stage.value}]{detail}{count} {event.message}".rstrip(),
    )


async def _entity_command(session: RebrickableSession, args: argparse.Namespace) -> int:
    if args.csv and not args.bom:
        raise ValueError("--csv requires --bom")
    repository = session.sets if args.command == "set" else session.minifigs
    identifier = args.set_num if args.command == "set" else args.fig_num
    if args.inventory:
        inventory = await repository.inventory(identifier, version=args.version)
        _print_entity(
            inventory, json_output=args.json, output_format=args.output_format
        )
    elif args.bom:
        bom = await repository.bill_of_materials(
            identifier, include_spares=args.include_spares
        )
        if args.csv or args.output_format == "csv":
            _write_stdout(catalog_bom_to_csv(bom.rows))
        else:
            _print_entity(bom, json_output=args.json, output_format=args.output_format)
        if bom.skipped and not args.json:
            for item in bom.skipped:
                _print_stderr(f"skipped {item.owner_num}: {item.reason}")
    else:
        entity = await repository.require(identifier)
        _print_entity(entity, json_output=args.json, output_format=args.output_format)
    return ExitCode.OK


async def _run(args: argparse.Namespace) -> int:
    if args.command == "url":
        builders = {"part": part_url, "set": set_url, "minifig": minifig_url}
        _print_stdout(builders[args.kind](args.identifier))
        return ExitCode.OK
    if args.command == "api-spec":
        resource = files("rebrickable.data").joinpath(OPENAPI_RESOURCE)
        raw = resource.read_bytes()
        if args.output:
            args.output.write_bytes(raw)
        else:
            _write_stdout(raw)
        return ExitCode.OK

    config = Config.load()
    if args.command == "api":
        return await _api_command(config, args)

    async with await RebrickableSession.open(config) as session:
        if args.command == "status":
            state = await session.state(verify=True)
            status = (
                ExitCode.OK
                if state.status is CatalogStatus.READY
                else ExitCode.MISSING_DATA
            )
            _print_entity(
                state,
                json_output=args.json,
                output_format=args.output_format,
                status=status,
            )
            return status
        if args.command == "refresh":
            report = await session.refresh_catalog(
                force=args.force, progress=None if args.json else _progress
            )
            _print_entity(
                report, json_output=args.json, output_format=args.output_format
            )
            return ExitCode.OK
        if args.command == "search":
            kinds = {SearchKind(item) for item in args.kind} if args.kind else None
            filters = SearchFilters(
                year_from=args.year_from,
                year_to=args.year_to,
                theme_id=args.theme_id,
                min_parts=args.min_parts,
                max_parts=args.max_parts,
                category_id=args.category_id,
                material=args.material,
                include_subthemes=args.include_subthemes,
            )
            result = await session.search(
                args.query,
                kinds=kinds,
                filters=filters,
                limit=args.limit,
                offset=args.offset,
            )
            if args.json or args.output_format == "json":
                _write_stdout(to_json(result, schema="rebrickable.search"))
            elif args.output_format == "yaml":
                _print_entity(result, output_format="yaml")
            elif args.output_format == "csv":
                _print_entity(result.hits, output_format="csv")
            else:
                for hit in result.hits:
                    _print_stdout(
                        f"{hit.kind.value:<14} {hit.canonical_id:<20} {hit.title}"
                    )
            return ExitCode.OK
        if args.command == "part":
            _check_part_flags(args)
            if args.usage:
                value = await session.parts.usage_stats(
                    args.part_num, color_id=args.color_id
                )
            elif args.sets:
                value = await session.parts.used_in_sets(
                    args.part_num,
                    color_id=args.color_id,
                    include_spares=args.include_spares,
                    limit=50 if args.limit is None else args.limit,
                    offset=0 if args.offset is None else args.offset,
                )
            elif args.relationships:
                value = await session.parts.relationships(args.part_num)
            else:
                value = await session.parts.require(args.part_num)
            _print_entity(
                value, json_output=args.json, output_format=args.output_format
            )
            return ExitCode.OK
        if args.command in {"set", "minifig"}:
            return await _entity_command(session, args)
        if args.command == "catalog":
            if args.catalog_command == "doctor":
                state = await session.state(verify=True)
                conflict = _package_version("pyrebrickable")
                report = {
                    "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version,
                    "rebrickable": _package_version("rebrickable"),
                    "pyrebrickable_conflict": conflict,
                    "catalog_status": state.status,
                    "database_path": state.database_path,
                    "api_key_configured": bool(config.api_key),
                    "diagnostics": state.diagnostics,
                }
                status = ExitCode.INVALID_INPUT if conflict else ExitCode.OK
                _print_entity(report, output_format=args.output_format, status=status)
                return status
            if args.catalog_command == "path":
                state = await session.state()
                status = (
                    ExitCode.OK
                    if state.status is CatalogStatus.READY
                    else ExitCode.MISSING_DATA
                )
                _print_stdout(state.database_path, status=status)
                return status
            if args.catalog_command == "versions":
                value = await session.inventories.versions(args.owner_num)
            else:
                value = await session.inventories.diff(
                    args.owner_num, args.before_version, args.after_version
                )
            _print_entity(value, output_format=args.output_format)
            return ExitCode.OK
        if args.command == "bom":
            if args.bom_command == "diff":
                value = _read_bom(args.before, args).diff(_read_bom(args.after, args))
                _print_entity(value, output_format=args.output_format)
                return ExitCode.OK
            if args.bom_command == "validate":
                bom = _read_bom(args.input, args)
                report = await session.validate_bom(bom)
                status = (
                    ExitCode.INCOMPLETE_TRANSLATION
                    if report.unavailable_count
                    else ExitCode.OK
                )
                _print_entity(report, output_format=args.output_format, status=status)
                return status
            value = _read_bom(args.input, args)
            _print_entity(value, output_format=args.output_format)
            return ExitCode.OK
        if args.command == "translate-ldraw":
            report = await session.ldraw.translate_model_path(
                args.model, library_path=args.ldraw_library
            )
            status = (
                ExitCode.OK
                if all(row.status is MappingStatus.RESOLVED for row in report.rows)
                else ExitCode.INCOMPLETE_TRANSLATION
            )
            if args.csv or args.output_format == "csv":
                _write_stdout(
                    translation_to_csv(report, unresolved_only=args.unresolved_only),
                    status=status,
                )
            elif args.json:
                value = report.incomplete_rows if args.unresolved_only else report
                _write_stdout(
                    to_json(value, schema="rebrickable.ldraw-translation"),
                    status=status,
                )
            else:
                _write_stdout(
                    translation_table(report, unresolved_only=args.unresolved_only),
                    status=status,
                )
            if not args.json:
                for note in report.diagnostics:
                    _print_stderr(f"[ldraw] {note}")
            return status
    return ExitCode.UNEXPECTED  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    """Run the CLI without taking ownership of the host's output streams."""
    return _main(argv, owned=())


def _entrypoint(argv: list[str] | None = None) -> int:
    """Run as an executable, owning only the original process output streams."""
    owned = tuple(
        (stream, descriptor)
        for name, descriptor in (("__stdout__", 1), ("__stderr__", 2))
        if (stream := getattr(sys, name, None)) is not None
    )
    return _main(argv, owned=owned)


def _main(argv: list[str] | None, *, owned: tuple[tuple[IO[str], int], ...]) -> int:
    token = _output_state.set(_OutputState(owned=owned))
    status = int(ExitCode.OK)
    parser_exit: SystemExit | None = None
    try:
        parser = _parser()
        # Normalize IntEnum exit codes to the public plain-int return contract.
        status = int(asyncio.run(_run(parser.parse_args(argv))))  # pyrefly: ignore[unnecessary-type-conversion]
    except SystemExit as exc:
        parser_exit = exc
    except _StdoutPipeClosedError as exc:
        status = int(exc.status)  # pyrefly: ignore[unnecessary-type-conversion]
    except (ValueError, argparse.ArgumentError, OptionalDependencyError) as exc:
        _print_stderr(str(exc))
        status = int(ExitCode.INVALID_INPUT)
    except (CatalogUnavailableError, EntityNotFoundError) as exc:
        _print_stderr(str(exc))
        status = int(ExitCode.MISSING_DATA)
    except ApiError as exc:
        _print_stderr(str(exc))
        status = int(ExitCode.API_FAILURE)
    except RebrickableError as exc:
        _print_stderr(str(exc))
        status = int(ExitCode.UNEXPECTED)
    except Exception as exc:  # noqa: BLE001 - final CLI diagnostic boundary
        _print_stderr(f"unexpected failure: {exc}")
        status = int(ExitCode.UNEXPECTED)

    finally:
        try:
            failure = _flush_stream(sys.stdout, role="stdout")
            if failure is not None and parser_exit is None:
                status = int(ExitCode.UNEXPECTED)
                _print_stderr(f"unable to flush stdout: {failure}")
            _flush_stream(sys.stderr, role="stderr")
        finally:
            _output_state.reset(token)
    if parser_exit is not None:
        raise parser_exit
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_entrypoint())
