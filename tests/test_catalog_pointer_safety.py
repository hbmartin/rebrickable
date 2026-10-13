from __future__ import annotations

import json
import shutil
from unittest.mock import Mock

import pytest

from rebrickable import CatalogStatus
from rebrickable.bridge.cache import store_crosswalks
from rebrickable.bridge.ldraw import _active_database, _persist_override
from rebrickable.bridge.overrides import read_overrides
from rebrickable.catalog.database import (
    CatalogPaths,
    _read_active_pointer,
    catalog_state,
)
from rebrickable.errors import CatalogUnavailableError


@pytest.mark.parametrize(
    "snapshot_id",
    [
        "../outside",
        "/outside",
        "C:outside",
        "a\\b",
        ".. ",
        "old.",
        "old ",
        "NUL",
        "con.txt",
        "NUL .txt",
        "COM1 .csv",
        "COM1",
        "LPT².csv",
        "CONIN$",
        "a\x01b",
        "a|b",
        "a?b",
        "a*b",
        "a<b",
        "a>b",
        'a"b',
        17,
        None,
    ],
)
async def test_all_pointer_readers_reject_unsafe_ids(
    catalog_config, monkeypatch, snapshot_id
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text(json.dumps({"snapshot_id": snapshot_id}))
    connect = Mock(side_effect=AssertionError("unsafe database was opened"))
    monkeypatch.setattr("rebrickable.bridge.cache.sqlite3.connect", connect)
    assert _read_active_pointer(paths).snapshot_id == ""
    assert _active_database(paths) is None
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.UNREADABLE
    with pytest.raises(CatalogUnavailableError):
        store_crosswalks(
            catalog_config,
            entity_kind="part",
            external_system="ldraw",
            canonical_id="3001",
            external_ids=("outside",),
            operation_id="op",
            response_payload={},
        )
    connect.assert_not_called()


@pytest.mark.parametrize("target", ["snapshot", "database", "manifest"])
async def test_pointer_links_cannot_escape_snapshot_boundary(
    catalog_config, tmp_path, target
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    snapshot = paths.snapshots_dir / "fixture-snapshot"
    external = tmp_path / "outside"
    shutil.copytree(snapshot, external)
    before = paths.database_for(snapshot.name).read_bytes()
    if target == "snapshot":
        shutil.rmtree(snapshot)
        link, destination = snapshot, external
    else:
        name = (
            catalog_config.database_path.name
            if target == "database"
            else "manifest.json"
        )
        link, destination = snapshot / name, external / name
        link.unlink()
    try:
        link.symlink_to(destination, target_is_directory=target == "snapshot")
    except OSError:
        pytest.skip("platform does not permit symbolic links")
    assert _read_active_pointer(paths).snapshot_id == ""
    assert _active_database(paths) is None
    assert (await catalog_state(catalog_config)).status is CatalogStatus.UNREADABLE
    with pytest.raises(CatalogUnavailableError):
        store_crosswalks(
            catalog_config,
            entity_kind="part",
            external_system="ldraw",
            canonical_id="3001",
            external_ids=("outside",),
            operation_id="op",
            response_payload={},
        )
    assert (external / catalog_config.database_path.name).read_bytes() == before


def test_unsafe_catalog_still_allows_yaml_only_override(
    catalog_config, monkeypatch
) -> None:

    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text(json.dumps({"snapshot_id": "../outside"}))
    connect = Mock(side_effect=AssertionError("outside database was opened"))
    monkeypatch.setattr("rebrickable.bridge.ldraw.sqlite3.connect", connect)
    _persist_override(
        path=catalog_config.mapping_overrides_path,
        config=catalog_config,
        entity_kind="part",
        source_id="yaml-only",
        target_id="3001",
        reason="test",
    )
    assert any(
        row["source_id"] == "yaml-only"
        for row in read_overrides(catalog_config.mapping_overrides_path)
    )
    connect.assert_not_called()
