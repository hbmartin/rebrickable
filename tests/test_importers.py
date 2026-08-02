from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from rebrickable.catalog.importers import DATASET_BY_NAME, inspect_header
from rebrickable.errors import DatasetSchemaError


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
