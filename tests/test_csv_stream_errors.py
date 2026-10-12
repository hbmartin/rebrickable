from __future__ import annotations

import csv
import gzip
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from rebrickable import Config
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.catalog.importers import (
    DATASET_BY_NAME,
    _rows,
    import_catalog,
    inspect_header,
)
from rebrickable.errors import DatasetSchemaError
from rebrickable.refresh import refresh_catalog
from tests.test_refresh_state import downloader, snapshot_sources


def corrupt_csv(path: Path, kind: str) -> type[Exception]:
    prefix = b"id,name\n1,Bricks\n" + b"".join(
        f"{i},Category {i}\n".encode() for i in range(2, 6000)
    )
    suffix = (
        b"6000,\xff\n"
        if kind == "unicode"
        else b"6000," + b"x" * (csv.field_size_limit() + 1) + b"\n"
    )
    path.write_bytes(gzip.compress(prefix + suffix))
    return UnicodeDecodeError if kind == "unicode" else csv.Error


@pytest.mark.parametrize("kind", ["unicode", "csv"])
def test_csv_iteration_wraps_late_errors(tmp_path: Path, kind: str) -> None:
    definition = DATASET_BY_NAME["part_categories"]
    path = tmp_path / definition.filename
    cause = corrupt_csv(path, kind)
    assert inspect_header(path, definition) == ()
    rows = _rows(path, definition)
    assert next(rows) == (1, "Bricks")
    with pytest.raises(
        DatasetSchemaError, match=r"part_categories.csv.gz:\d+:"
    ) as error:
        list(rows)
    assert isinstance(error.value.__cause__, cause)


@pytest.mark.parametrize("kind", ["unicode", "csv"])
def test_csv_import_rolls_back_and_releases_connection(
    catalog_config: Config, tmp_path: Path, kind: str
) -> None:
    files = snapshot_sources(catalog_config)
    definition = DATASET_BY_NAME["part_categories"]
    files[definition.name] = tmp_path / definition.filename
    corrupt_csv(files[definition.name], kind)
    database = tmp_path / "candidate.sqlite"
    with pytest.raises(DatasetSchemaError):
        import_catalog(
            files,
            database,
            snapshot_id="candidate",
            retrieved_at="2026-08-01T00:00:00+00:00",
        )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM part_categories"
        ).fetchone() == (0,)
        connection.execute("BEGIN EXCLUSIVE")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unicode", "csv"])
async def test_csv_refresh_failure_preserves_previous_snapshot(
    catalog_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    files = snapshot_sources(catalog_config)
    path = tmp_path / "part_categories.csv.gz"
    corrupt_csv(path, kind)
    files["part_categories"] = path
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(files))
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    with pytest.raises(DatasetSchemaError):
        await refresh_catalog(catalog_config)
    assert paths.active_pointer.read_bytes() == pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).snapshot_id == "fixture-snapshot"
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
