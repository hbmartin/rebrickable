"""Streaming CSV-to-SQLite catalog import."""

from __future__ import annotations

import csv
import gzip
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from rebrickable.catalog.schema import SCHEMA_SQL, SCHEMA_VERSION
from rebrickable.errors import DatasetIntegrityError, DatasetSchemaError

_RGB = re.compile(r"[0-9A-Fa-f]{6}")
ProgressReporter = Callable[[str, int], None]


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    name: str
    required: tuple[str, ...]
    insert_sql: str
    transform: Callable[[Mapping[str, str]], tuple[object, ...]]

    @property
    def filename(self) -> str:
        return f"{self.name}.csv.gz"


def _text(value: str) -> str:
    return value


def _nullable_text(value: str) -> str | None:
    return value or None


def _integer(value: str) -> int:
    return int(value)


def _nullable_integer(value: str) -> int | None:
    return None if value == "" else int(value)


def _boolean(value: str) -> int:
    token = value.casefold()
    if token == "true":
        return 1
    if token == "false":
        return 0
    raise ValueError(f"invalid boolean token {value!r}")


def _quantity(value: str) -> int:
    quantity = int(value)
    if quantity < 0:
        raise ValueError("quantity must not be negative")
    return quantity


def _rgb(value: str) -> str:
    if _RGB.fullmatch(value) is None:
        raise ValueError(f"invalid RGB value {value!r}")
    return value.upper()


DATASETS: tuple[DatasetDefinition, ...] = (
    DatasetDefinition(
        "themes",
        ("id", "name", "parent_id"),
        "INSERT INTO themes VALUES (?, ?, ?)",
        lambda r: (
            _integer(r["id"]),
            _text(r["name"]),
            _nullable_integer(r["parent_id"]),
        ),
    ),
    DatasetDefinition(
        "colors",
        ("id", "name", "rgb", "is_trans", "num_parts", "num_sets", "y1", "y2"),
        "INSERT INTO colors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        lambda r: (
            _integer(r["id"]),
            _text(r["name"]),
            _rgb(r["rgb"]),
            _boolean(r["is_trans"]),
            _quantity(r["num_parts"]),
            _quantity(r["num_sets"]),
            _nullable_integer(r["y1"]),
            _nullable_integer(r["y2"]),
        ),
    ),
    DatasetDefinition(
        "part_categories",
        ("id", "name"),
        "INSERT INTO part_categories VALUES (?, ?)",
        lambda r: (_integer(r["id"]), _text(r["name"])),
    ),
    DatasetDefinition(
        "parts",
        ("part_num", "name", "part_cat_id", "part_material"),
        "INSERT INTO parts VALUES (?, ?, ?, ?)",
        lambda r: (
            _text(r["part_num"]),
            _text(r["name"]),
            _integer(r["part_cat_id"]),
            _text(r["part_material"]),
        ),
    ),
    DatasetDefinition(
        "part_relationships",
        ("rel_type", "child_part_num", "parent_part_num"),
        "INSERT INTO part_relationships VALUES (?, ?, ?)",
        lambda r: (
            _text(r["rel_type"]),
            _text(r["child_part_num"]),
            _text(r["parent_part_num"]),
        ),
    ),
    DatasetDefinition(
        "elements",
        ("element_id", "part_num", "color_id", "design_id"),
        "INSERT INTO elements VALUES (?, ?, ?, ?)",
        lambda r: (
            _text(r["element_id"]),
            _text(r["part_num"]),
            _integer(r["color_id"]),
            _text(r["design_id"]),
        ),
    ),
    DatasetDefinition(
        "sets",
        ("set_num", "name", "year", "theme_id", "num_parts", "img_url"),
        "INSERT INTO sets VALUES (?, ?, ?, ?, ?, ?)",
        lambda r: (
            _text(r["set_num"]),
            _text(r["name"]),
            _integer(r["year"]),
            _integer(r["theme_id"]),
            _quantity(r["num_parts"]),
            _nullable_text(r["img_url"]),
        ),
    ),
    DatasetDefinition(
        "minifigs",
        ("fig_num", "name", "num_parts", "img_url"),
        "INSERT INTO minifigs VALUES (?, ?, ?, ?)",
        lambda r: (
            _text(r["fig_num"]),
            _text(r["name"]),
            _quantity(r["num_parts"]),
            _nullable_text(r["img_url"]),
        ),
    ),
    DatasetDefinition(
        "inventories",
        ("id", "version", "set_num"),
        "INSERT INTO inventories VALUES (?, ?, ?)",
        lambda r: (_integer(r["id"]), _integer(r["version"]), _text(r["set_num"])),
    ),
    DatasetDefinition(
        "inventory_parts",
        ("inventory_id", "part_num", "color_id", "quantity", "is_spare", "img_url"),
        "INSERT INTO inventory_parts VALUES (?, ?, ?, ?, ?, ?)",
        lambda r: (
            _integer(r["inventory_id"]),
            _text(r["part_num"]),
            _integer(r["color_id"]),
            _quantity(r["quantity"]),
            _boolean(r["is_spare"]),
            _nullable_text(r["img_url"]),
        ),
    ),
    DatasetDefinition(
        "inventory_sets",
        ("inventory_id", "set_num", "quantity"),
        "INSERT INTO inventory_sets VALUES (?, ?, ?)",
        lambda r: (
            _integer(r["inventory_id"]),
            _text(r["set_num"]),
            _quantity(r["quantity"]),
        ),
    ),
    DatasetDefinition(
        "inventory_minifigs",
        ("inventory_id", "fig_num", "quantity"),
        "INSERT INTO inventory_minifigs VALUES (?, ?, ?)",
        lambda r: (
            _integer(r["inventory_id"]),
            _text(r["fig_num"]),
            _quantity(r["quantity"]),
        ),
    ),
)

DATASET_BY_NAME = {dataset.name: dataset for dataset in DATASETS}


def inspect_header(path: Path, definition: DatasetDefinition) -> tuple[str, ...]:
    try:
        with gzip.open(path, mode="rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
    except (OSError, EOFError, UnicodeError, csv.Error, StopIteration) as exc:
        raise DatasetSchemaError(
            f"unable to read {definition.filename}: {exc}"
        ) from exc
    missing = set(definition.required) - set(header)
    if missing:
        raise DatasetSchemaError(
            f"{definition.filename} missing required columns: {', '.join(sorted(missing))}",
        )
    return tuple(column for column in header if column not in definition.required)


def _rows(path: Path, definition: DatasetDefinition) -> Iterator[tuple[object, ...]]:
    with gzip.open(path, mode="rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            try:
                yield definition.transform(row)
            except (KeyError, TypeError, ValueError) as exc:
                raise DatasetSchemaError(
                    f"{definition.filename}:{line_number}: {exc}",
                ) from exc


def _batch(
    rows: Iterator[tuple[object, ...]], size: int = 5_000
) -> Iterator[list[tuple[object, ...]]]:
    pending: list[tuple[object, ...]] = []
    for row in rows:
        pending.append(row)
        if len(pending) >= size:
            yield pending
            pending = []
    if pending:
        yield pending


def _first_missing(connection: sqlite3.Connection, query: str) -> str | None:
    row = connection.execute(query).fetchone()
    return None if row is None else str(row[0])


def validate_references(connection: sqlite3.Connection) -> None:
    checks = {
        "theme parent": "SELECT t.parent_id FROM themes t LEFT JOIN themes p ON p.id=t.parent_id WHERE t.parent_id IS NOT NULL AND p.id IS NULL LIMIT 1",
        "part category": "SELECT p.part_num FROM parts p LEFT JOIN part_categories c ON c.id=p.part_cat_id WHERE c.id IS NULL LIMIT 1",
        "relationship child": "SELECT r.child_part_num FROM part_relationships r LEFT JOIN parts p ON p.part_num=r.child_part_num WHERE p.part_num IS NULL LIMIT 1",
        "relationship parent": "SELECT r.parent_part_num FROM part_relationships r LEFT JOIN parts p ON p.part_num=r.parent_part_num WHERE p.part_num IS NULL LIMIT 1",
        "element part": "SELECT e.element_id FROM elements e LEFT JOIN parts p ON p.part_num=e.part_num WHERE p.part_num IS NULL LIMIT 1",
        "element color": "SELECT e.element_id FROM elements e LEFT JOIN colors c ON c.id=e.color_id WHERE e.color_id>=0 AND c.id IS NULL LIMIT 1",
        "set theme": "SELECT s.set_num FROM sets s LEFT JOIN themes t ON t.id=s.theme_id WHERE t.id IS NULL LIMIT 1",
        "inventory owner": "SELECT i.owner_num FROM inventories i LEFT JOIN sets s ON s.set_num=i.owner_num LEFT JOIN minifigs f ON f.fig_num=i.owner_num WHERE s.set_num IS NULL AND f.fig_num IS NULL LIMIT 1",
        "inventory part inventory": "SELECT ip.inventory_id FROM inventory_parts ip LEFT JOIN inventories i ON i.id=ip.inventory_id WHERE i.id IS NULL LIMIT 1",
        "inventory part": "SELECT ip.part_num FROM inventory_parts ip LEFT JOIN parts p ON p.part_num=ip.part_num WHERE p.part_num IS NULL LIMIT 1",
        "inventory color": "SELECT ip.color_id FROM inventory_parts ip LEFT JOIN colors c ON c.id=ip.color_id WHERE ip.color_id>=0 AND c.id IS NULL LIMIT 1",
        "contained set": "SELECT x.set_num FROM inventory_sets x LEFT JOIN sets s ON s.set_num=x.set_num WHERE s.set_num IS NULL LIMIT 1",
        "contained minifig": "SELECT x.fig_num FROM inventory_minifigs x LEFT JOIN minifigs f ON f.fig_num=x.fig_num WHERE f.fig_num IS NULL LIMIT 1",
    }
    failures = [
        f"{label}: {value}"
        for label, query in checks.items()
        if (value := _first_missing(connection, query))
    ]
    if failures:
        raise DatasetIntegrityError(
            "cross-file references failed: " + "; ".join(failures)
        )


def build_search_index(connection: sqlite3.Connection) -> None:
    statements = (
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name,category_id,material) SELECT 'part',part_num,name,'part','',lower(name),part_cat_id,material FROM parts",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name,year,theme_id,num_parts) SELECT 'set',set_num,name,CAST(year AS TEXT),'',lower(name),year,theme_id,num_parts FROM sets",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name,num_parts) SELECT 'minifig',fig_num,name,'minifigure','',lower(name),num_parts FROM minifigs",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name) SELECT 'theme',CAST(id AS TEXT),name,'theme','',lower(name) FROM themes",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name) SELECT 'part_category',CAST(id AS TEXT),name,'part category','',lower(name) FROM part_categories",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name) SELECT 'color',CAST(id AS TEXT),name,rgb,'',lower(name) FROM colors",
        "INSERT INTO search_documents(kind,canonical_id,title,subtitle,external_ids,normalized_name) SELECT 'element',element_id,element_id,part_num,design_id,lower(element_id) FROM elements",
    )
    for statement in statements:
        connection.execute(statement)
    connection.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")


def import_catalog(
    files: Mapping[str, Path],
    database_path: Path,
    *,
    snapshot_id: str,
    retrieved_at: str,
    reporter: ProgressReporter | None = None,
) -> tuple[dict[str, int], dict[str, tuple[str, ...]]]:
    row_counts: dict[str, int] = {}
    unknown_columns: dict[str, tuple[str, ...]] = {}
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN")
        for definition in DATASETS:
            path = files[definition.name]
            unknown_columns[definition.name] = inspect_header(path, definition)
            count = 0
            for batch in _batch(_rows(path, definition)):
                connection.executemany(definition.insert_sql, batch)
                count += len(batch)
                if reporter is not None:
                    reporter(definition.name, count)
            row_counts[definition.name] = count
        validate_references(connection)
        build_search_index(connection)
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "snapshot_id": snapshot_id,
            "retrieved_at": retrieved_at,
            "row_counts": json.dumps(row_counts, sort_keys=True),
        }
        connection.executemany(
            "INSERT INTO snapshot_meta VALUES (?, ?)", metadata.items()
        )
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise DatasetIntegrityError("SQLite integrity_check failed")
        connection.execute("PRAGMA optimize")
    except (sqlite3.Error, OSError, EOFError) as exc:
        connection.rollback()
        raise DatasetIntegrityError(str(exc)) from exc
    finally:
        connection.close()
    return row_counts, unknown_columns
