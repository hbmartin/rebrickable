from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rebrickable.bridge.overrides import materialize_overrides, write_overrides
from rebrickable.catalog.database import CatalogPaths
from rebrickable.catalog.importers import DATASET_BY_NAME, import_catalog
from rebrickable.catalog.schema import SCHEMA_VERSION
from rebrickable.config import Config
from rebrickable.types import FileFingerprint

ROWS = {
    "themes": [(1, "Space", "")],
    "colors": [
        (4, "Red", "C91A09", "True", 2, 2, 1978, 2026),
        (1, "Black", "05131D", "False", 1, 1, 1978, 2026),
    ],
    "part_categories": [(1, "Bricks")],
    "parts": [
        ("3001", "Brick 2 x 4", 1, "Plastic"),
        ("3002", "Brick 2 x 3", 1, "Plastic"),
    ],
    "part_relationships": [("A", "3002", "3001")],
    "elements": [("3001004", "3001", 4, "3001"), ("3002004", "3002", 4, "3002")],
    "sets": [
        ("100-1", "Root Set", 2024, 1, 6, "https://images.example/root.png"),
        ("200-1", "Child Set", 2020, 1, 1, ""),
    ],
    "minifigs": [("fig-1", "Astronaut", 1, "")],
    "inventories": [(1, 1, "100-1"), (2, 2, "100-1"), (3, 1, "200-1"), (4, 1, "fig-1")],
    "inventory_parts": [
        (1, "3001", 4, 99, "False", ""),
        (2, "3001", 4, 2, "False", ""),
        (2, "3002", 4, 1, "True", ""),
        (3, "3001", 1, 1, "False", ""),
        (4, "3002", 4, 1, "False", ""),
    ],
    "inventory_sets": [(2, "200-1", 2)],
    "inventory_minifigs": [(2, "fig-1", 1)],
}


def write_dataset(
    directory: Path,
    name: str,
    rows: list[tuple[object, ...]],
    *,
    extra: str | None = None,
) -> Path:
    definition = DATASET_BY_NAME[name]
    path = directory / definition.filename
    with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        header = [*definition.required, *([extra] if extra else [])]
        writer.writerow(header)
        for row in rows:
            writer.writerow([*row, *(["ignored"] if extra else [])])
    return path


@pytest.fixture
def catalog_config(tmp_path: Path) -> Config:
    database_path = tmp_path / "data" / "catalog.sqlite"
    cache_path = tmp_path / "cache"
    overrides = tmp_path / "config" / "mappings.yml"
    write_overrides(
        overrides,
        (
            {
                "entity_kind": "part",
                "source_system": "ldraw",
                "source_id": "3001",
                "target_system": "rebrickable",
                "target_id": "3001",
                "reason": "fixture",
            },
            {
                "entity_kind": "color",
                "source_system": "ldraw",
                "source_id": "4",
                "target_system": "rebrickable",
                "target_id": "4",
                "reason": "fixture",
            },
        ),
    )
    config = Config(
        database_path=database_path,
        cache_path=cache_path,
        mapping_overrides_path=overrides,
    )
    paths = CatalogPaths.from_config(config)
    snapshot_id = "fixture-snapshot"
    snapshot = paths.snapshots_dir / snapshot_id
    snapshot.mkdir(parents=True)
    files = {
        name: write_dataset(
            snapshot, name, rows, extra="future_column" if name == "parts" else None
        )
        for name, rows in ROWS.items()
    }
    row_counts, unknown = import_catalog(
        files,
        snapshot / database_path.name,
        snapshot_id=snapshot_id,
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
    )
    materialize_overrides(overrides, snapshot / database_path.name)
    fingerprints = []
    for name, source in files.items():
        raw = source.read_bytes()
        fingerprints.append(
            FileFingerprint(
                name,
                f"https://example.test/{source.name}",
                None,
                None,
                len(raw),
                hashlib.sha256(raw).hexdigest(),
                row_counts[name],
                unknown[name],
            ),
        )
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "retrieved_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
                "files": [asdict(item) for item in fingerprints],
            },
        ),
        encoding="utf-8",
    )
    paths.active_pointer.write_text(
        json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8"
    )
    return config
