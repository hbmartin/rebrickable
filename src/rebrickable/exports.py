"""Deterministic machine and human serialization."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from rebrickable.bridge.models import TranslationReport
from rebrickable.catalog.models import BomRow
from rebrickable.types import MappingStatus

JSON_SCHEMA_VERSION = 1
_SECRET_FIELDS = frozenset({"api_key", "password", "user_token"})
TRANSLATION_FIELDS = (
    "ldraw_part_num",
    "ldraw_color_code",
    "rebrickable_part_num",
    "rebrickable_color_id",
    "quantity",
    "status",
    "part_match_source",
    "color_match_source",
    "candidates",
    "notes",
)


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            key: _json_value(item)
            for key, item in asdict(value).items()
            if key.casefold() not in _SECRET_FIELDS
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
            if str(key).casefold() not in _SECRET_FIELDS
        }
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_csv_formula(value: str) -> str:
    """Neutralize cells spreadsheets would evaluate as formulas."""
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def to_json(value: Any, *, schema: str = "rebrickable") -> str:
    envelope = {
        "schema": schema,
        "schema_version": JSON_SCHEMA_VERSION,
        "data": _json_value(value),
    }
    return (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def translation_to_csv(
    report: TranslationReport, *, unresolved_only: bool = False
) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=TRANSLATION_FIELDS, lineterminator="\r\n"
    )
    writer.writeheader()
    for row in report.rows:
        if unresolved_only and row.status is MappingStatus.RESOLVED:
            continue
        candidates = sorted(
            {
                candidate.identifier
                for candidate in (
                    *row.part_match.candidates,
                    *row.color_match.candidates,
                )
            },
        )
        writer.writerow(
            {
                "ldraw_part_num": escape_csv_formula(row.ldraw_part_num),
                "ldraw_color_code": row.ldraw_color_code,
                "rebrickable_part_num": escape_csv_formula(
                    row.rebrickable_part_num or ""
                ),
                "rebrickable_color_id": row.rebrickable_color_id
                if row.rebrickable_color_id is not None
                else "",
                "quantity": row.quantity,
                "status": row.status.value,
                "part_match_source": row.part_match.source.value,
                "color_match_source": row.color_match.source.value,
                "candidates": escape_csv_formula("|".join(candidates)),
                "notes": escape_csv_formula(
                    f"{row.part_match.explanation}; {row.color_match.explanation}"
                ),
            },
        )
    return output.getvalue()


def translation_table(
    report: TranslationReport, *, unresolved_only: bool = False
) -> str:
    lines = ["LDRAW PART  COLOR  REBRICKABLE PART  COLOR  QTY  STATUS"]
    for row in report.rows:
        if unresolved_only and row.status is MappingStatus.RESOLVED:
            continue
        lines.append(
            f"{row.ldraw_part_num:<11} {row.ldraw_color_code:<6} "
            f"{(row.rebrickable_part_num or '-'):<17} "
            f"{str(row.rebrickable_color_id) if row.rebrickable_color_id is not None else '-':<6} "
            f"{row.quantity:<4} {row.status.value}",
        )
    return "\n".join(lines) + "\n"


def catalog_bom_to_csv(rows: Iterable[BomRow]) -> str:
    """Serialize a flattened catalog BOM using stable RFC 4180 columns."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("part_num", "color_id", "quantity"))
    for row in rows:
        writer.writerow(
            (escape_csv_formula(row.part.part_num), row.color.id, row.quantity)
        )
    return output.getvalue()


def python_lookup_snippet(kind: str, identifier: str) -> str:
    """Return a ready-to-run bounded lookup using only public imports."""
    facades = {"part": "parts", "set": "sets", "minifig": "minifigs"}
    try:
        facade = facades[kind]
    except KeyError as exc:
        raise ValueError("kind must be part, set, or minifig") from exc
    return (
        "import asyncio\n"
        "from rebrickable import RebrickableSession\n\n"
        "async def main() -> None:\n"
        "    async with await RebrickableSession.open() as session:\n"
        f"        entity = await session.{facade}.require({identifier!r})\n"
        "        print(entity, entity.page_url)\n\n"
        "asyncio.run(main())\n"
    )
