from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from rebrickable.catalog.importers import (
    DATASET_BY_NAME,
    import_catalog,
    inspect_header,
)
from rebrickable.errors import DatasetIntegrityError, DatasetSchemaError
from rebrickable.refresh import _carry_crosswalk

from .conftest import ROWS, write_dataset


def test_missing_header_column(tmp_path: Path) -> None:
    path = tmp_path / "parts.csv.gz"
    with gzip.open(path, "wt") as handle:
        handle.write("part_num,name\n3001,Brick\n")
    with pytest.raises(DatasetSchemaError, match="missing required columns"):
        inspect_header(path, DATASET_BY_NAME["parts"])


def test_corrupt_gzip(tmp_path: Path) -> None:
    path = tmp_path / "colors.csv.gz"
    path.write_text("bad")
    with pytest.raises(DatasetSchemaError):
        inspect_header(path, DATASET_BY_NAME["colors"])


@pytest.mark.parametrize(
    ("dataset", "rows", "label"),
    [
        ("inventory_sets", [(77777, "200-1", 2)], "contained set inventory"),
        ("inventory_minifigs", [(77777, "fig-1", 1)], "contained minifig inventory"),
    ],
)
def test_orphaned_inventory_links_fail_validation(
    tmp_path: Path,
    dataset: str,
    rows: list[tuple[object, ...]],
    label: str,
) -> None:
    data = {**ROWS, dataset: rows}
    files = {
        name: write_dataset(tmp_path, name, values) for name, values in data.items()
    }
    with pytest.raises(DatasetIntegrityError, match=label):
        import_catalog(
            files,
            tmp_path / "catalog.sqlite",
            snapshot_id="broken",
            retrieved_at="2026-08-01T00:00:00+00:00",
        )


def test_import_catalog_build_fts_flag(tmp_path: Path) -> None:
    files = {name: write_dataset(tmp_path, name, rows) for name, rows in ROWS.items()}
    database = tmp_path / "catalog.sqlite"
    import_catalog(
        files,
        database,
        snapshot_id="deferred-fts",
        retrieved_at="2026-08-01T00:00:00+00:00",
        build_fts=False,
    )
    connection = sqlite3.connect(database)
    try:
        query = "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'brick'"
        assert connection.execute(query).fetchone()[0] == 0
    finally:
        connection.close()
    _carry_crosswalk(None, database)
    connection = sqlite3.connect(database)
    try:
        query = "SELECT count(*) FROM search_fts WHERE search_fts MATCH 'brick'"
        assert connection.execute(query).fetchone()[0] > 0
    finally:
        connection.close()
