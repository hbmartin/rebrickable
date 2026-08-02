"""Non-interactive command-line facade."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from rebrickable.config import Config
from rebrickable.data import OPENAPI_RESOURCE
from rebrickable.errors import (
    ApiError,
    CatalogUnavailableError,
    EntityNotFoundError,
    OptionalDependencyError,
    RebrickableError,
)
from rebrickable.exports import to_json, translation_table, translation_to_csv
from rebrickable.progress import ProgressEvent
from rebrickable.session import RebrickableSession
from rebrickable.types import CatalogStatus, ExitCode, MappingStatus, SearchKind
from rebrickable.urls import minifig_url, part_url, set_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebrickable")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="inspect local catalog state")
    status.add_argument("--json", action="store_true")

    refresh = sub.add_parser("refresh", help="explicitly refresh all catalog datasets")
    refresh.add_argument("--force", action="store_true")
    refresh.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="search the local catalog")
    search.add_argument("query")
    search.add_argument(
        "--kind", action="append", choices=[kind.value for kind in SearchKind]
    )
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--json", action="store_true")

    part = sub.add_parser("part", help="show one part")
    part.add_argument("part_num")
    part.add_argument("--json", action="store_true")

    for name, identifier in (("set", "set_num"), ("minifig", "fig_num")):
        entity = sub.add_parser(name, help=f"show one {name}")
        entity.add_argument(identifier)
        mode = entity.add_mutually_exclusive_group()
        mode.add_argument("--inventory", action="store_true")
        mode.add_argument("--bom", action="store_true")
        entity.add_argument("--include-spares", action="store_true")
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

    spec = sub.add_parser("api-spec", help="print the vendored OpenAPI document")
    spec.add_argument("--output", type=Path)
    return parser


def _print_entity(value: Any, *, json_output: bool) -> None:
    if json_output:
        sys.stdout.write(to_json(value, schema="rebrickable.entity"))
    else:
        print(value)


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
        inventory = await repository.inventory(identifier)
        _print_entity(inventory, json_output=args.json)
    elif args.bom:
        bom = await repository.bill_of_materials(
            identifier, include_spares=args.include_spares
        )
        if args.csv:
            lines = ["part_num,color_id,quantity"]
            lines.extend(
                f"{row.part.part_num},{row.color.id},{row.quantity}" for row in bom
            )
            sys.stdout.write("\r\n".join(lines) + "\r\n")
        else:
            _print_entity(bom, json_output=args.json)
    else:
        entity = await repository.require(identifier)
        _print_entity(entity, json_output=args.json)
    return ExitCode.OK


async def _run(args: argparse.Namespace) -> int:
    if args.command == "url":
        builders = {"part": part_url, "set": set_url, "minifig": minifig_url}
        print(builders[args.kind](args.identifier))
        return ExitCode.OK
    if args.command == "api-spec":
        resource = files("rebrickable.data").joinpath(OPENAPI_RESOURCE)
        text = resource.read_text(encoding="utf-8")
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            parsed = json.loads(text)
            sys.stdout.write(
                json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n"
            )
        return ExitCode.OK

    async with await RebrickableSession.open(Config.load()) as session:
        if args.command == "status":
            state = await session.state()
            _print_entity(state, json_output=args.json)
            return (
                ExitCode.OK
                if state.status is CatalogStatus.READY
                else ExitCode.MISSING_DATA
            )
        if args.command == "refresh":
            report = await session.refresh_catalog(
                force=args.force, progress=None if args.json else _progress
            )
            _print_entity(report, json_output=args.json)
            return ExitCode.OK
        if args.command == "search":
            kinds = {SearchKind(item) for item in args.kind} if args.kind else None
            result = await session.search(args.query, kinds=kinds, limit=args.limit)
            if args.json:
                sys.stdout.write(to_json(result, schema="rebrickable.search"))
            else:
                for hit in result.hits:
                    print(f"{hit.kind.value:<14} {hit.canonical_id:<20} {hit.title}")
            return ExitCode.OK
        if args.command == "part":
            _print_entity(
                await session.parts.require(args.part_num), json_output=args.json
            )
            return ExitCode.OK
        if args.command in {"set", "minifig"}:
            return await _entity_command(session, args)
        if args.command == "translate-ldraw":
            report = await session.ldraw.translate_model_path(args.model)
            if args.csv:
                sys.stdout.write(
                    translation_to_csv(report, unresolved_only=args.unresolved_only)
                )
            elif args.json:
                value = report.unresolved_rows if args.unresolved_only else report
                sys.stdout.write(to_json(value, schema="rebrickable.ldraw-translation"))
            else:
                sys.stdout.write(translation_table(report))
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
