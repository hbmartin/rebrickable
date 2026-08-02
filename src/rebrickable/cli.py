"""Non-interactive command-line facade."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import platform
import sqlite3
import sys
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from typing import Any

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebrickable")
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
    part.add_argument("--limit", type=int, default=50)
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


def _print_entity(
    value: Any, *, json_output: bool = False, output_format: str = "table"
) -> None:
    if json_output or output_format == "json":
        sys.stdout.write(to_json(value, schema="rebrickable.entity"))
    elif output_format == "yaml":
        data = json.loads(to_json(value, schema="rebrickable.entity"))["data"]
        sys.stdout.write(yaml.safe_dump(data, sort_keys=True))
    elif output_format == "csv":
        sys.stdout.write(to_csv(value))
    else:
        print(value)


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
    print(
        f"[{event.stage.value}]{detail}{count} {event.message}".rstrip(),
        file=sys.stderr,
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
            sys.stdout.write(catalog_bom_to_csv(bom.rows))
        else:
            _print_entity(bom, json_output=args.json, output_format=args.output_format)
        if bom.skipped and not args.json:
            for item in bom.skipped:
                print(f"skipped {item.owner_num}: {item.reason}", file=sys.stderr)
    else:
        entity = await repository.require(identifier)
        _print_entity(entity, json_output=args.json, output_format=args.output_format)
    return ExitCode.OK


async def _run(args: argparse.Namespace) -> int:
    if args.command == "url":
        builders = {"part": part_url, "set": set_url, "minifig": minifig_url}
        print(builders[args.kind](args.identifier))
        return ExitCode.OK
    if args.command == "api-spec":
        resource = files("rebrickable.data").joinpath(OPENAPI_RESOURCE)
        raw = resource.read_bytes()
        if args.output:
            args.output.write_bytes(raw)
        else:
            sys.stdout.buffer.write(raw)
            sys.stdout.flush()
        return ExitCode.OK

    config = Config.load()
    if args.command == "api":
        return await _api_command(config, args)

    async with await RebrickableSession.open(config) as session:
        if args.command == "status":
            state = await session.state(verify=True)
            _print_entity(
                state, json_output=args.json, output_format=args.output_format
            )
            return (
                ExitCode.OK
                if state.status is CatalogStatus.READY
                else ExitCode.MISSING_DATA
            )
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
                sys.stdout.write(to_json(result, schema="rebrickable.search"))
            elif args.output_format == "yaml":
                _print_entity(result, output_format="yaml")
            elif args.output_format == "csv":
                _print_entity(result.hits, output_format="csv")
            else:
                for hit in result.hits:
                    print(f"{hit.kind.value:<14} {hit.canonical_id:<20} {hit.title}")
            return ExitCode.OK
        if args.command == "part":
            if args.usage:
                value = await session.parts.usage_stats(
                    args.part_num, color_id=args.color_id
                )
            elif args.sets:
                value = await session.parts.used_in_sets(
                    args.part_num,
                    color_id=args.color_id,
                    include_spares=args.include_spares,
                    limit=args.limit,
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
                _print_entity(report, output_format=args.output_format)
                return ExitCode.INVALID_INPUT if conflict else ExitCode.OK
            if args.catalog_command == "path":
                state = await session.state()
                print(state.database_path)
                return (
                    ExitCode.OK
                    if state.status is CatalogStatus.READY
                    else ExitCode.MISSING_DATA
                )
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
                _print_entity(report, output_format=args.output_format)
                return (
                    ExitCode.INCOMPLETE_TRANSLATION
                    if report.unavailable_count
                    else ExitCode.OK
                )
            value = _read_bom(args.input, args)
            _print_entity(value, output_format=args.output_format)
            return ExitCode.OK
        if args.command == "translate-ldraw":
            report = await session.ldraw.translate_model_path(
                args.model, library_path=args.ldraw_library
            )
            if args.csv or args.output_format == "csv":
                sys.stdout.write(
                    translation_to_csv(report, unresolved_only=args.unresolved_only)
                )
            elif args.json:
                value = report.incomplete_rows if args.unresolved_only else report
                sys.stdout.write(to_json(value, schema="rebrickable.ldraw-translation"))
            else:
                sys.stdout.write(
                    translation_table(report, unresolved_only=args.unresolved_only)
                )
            if not args.json:
                for note in report.diagnostics:
                    print(f"[ldraw] {note}", file=sys.stderr)
            return (
                ExitCode.OK
                if all(row.status is MappingStatus.RESOLVED for row in report.rows)
                else ExitCode.INCOMPLETE_TRANSLATION
            )
    return ExitCode.UNEXPECTED  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        return int(asyncio.run(_run(parser.parse_args(argv))))
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, io.UnsupportedOperation):
            pass
        return int(ExitCode.OK)
    except (ValueError, argparse.ArgumentError, OptionalDependencyError) as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.INVALID_INPUT)
    except (CatalogUnavailableError, EntityNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.MISSING_DATA)
    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.API_FAILURE)
    except RebrickableError as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.UNEXPECTED)
    except Exception as exc:  # pragma: no cover - last-resort CLI boundary
        print(f"unexpected failure: {exc}", file=sys.stderr)
        return int(ExitCode.UNEXPECTED)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
