from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from filelock import Timeout

from rebrickable import (
    Bom,
    CatalogStatus,
    ColorRef,
    PartRef,
    RebrickableSession,
    RelationshipType,
    SearchFilters,
    SearchKind,
)
from rebrickable.bom import BomItem
from rebrickable.catalog.database import CatalogPaths, catalog_state, open_catalog
from rebrickable.config import Config
from rebrickable.errors import (
    CatalogUnavailableError,
    CatalogUnreadableError,
    EntityNotFoundError,
)

from .conftest import build_catalog_config


@pytest.mark.asyncio
async def test_missing_catalog_state_and_query(tmp_path: Path) -> None:
    config = Config(
        database_path=tmp_path / "catalog.sqlite", cache_path=tmp_path / "cache"
    )
    state = await catalog_state(config)
    assert state.status is CatalogStatus.MISSING
    async with await RebrickableSession.open(config) as session:
        with pytest.raises(CatalogUnavailableError):
            await session.parts.get("3001")

    nested = Config(
        database_path=tmp_path / "absent" / "catalog.sqlite",
        cache_path=tmp_path / "cache",
    )
    with pytest.raises(CatalogUnavailableError):
        await open_catalog(nested)


@pytest.mark.asyncio
async def test_repository_search_inventory_and_bom(catalog_config: Config) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        state = await session.state()
        assert state.status is CatalogStatus.READY
        parts_file = next(item for item in state.files if item.dataset == "parts")
        assert parts_file.unknown_columns == ("future_column",)
        part = await session.parts.require("3001")
        assert part.name == "Brick 2 x 4"
        assert part.page_url.endswith("/parts/3001/")
        assert await session.parts.get("missing") is None
        with pytest.raises(EntityNotFoundError):
            await session.parts.require("missing")
        result = await session.search("3001", kinds={SearchKind.PART})
        assert result.hits[0].canonical_id == "3001"
        assert result.snapshot_id == "fixture-snapshot"
        inventory = await session.sets.inventory("100-1")
        assert inventory.version == 2
        assert inventory.parts[0].quantity == 2
        bom = await session.sets.bill_of_materials("100-1")
        quantities = {(row.part.part_num, row.color.id): row.quantity for row in bom}
        assert quantities == {("3001", 1): 2, ("3001", 4): 2, ("3002", 4): 1}
        assert len(bom) == len(bom.rows)
        assert bom[0] == bom.rows[0]
        assert bom[:] == bom.rows
        assert bom.skipped == ()
        with_spares = await session.sets.bill_of_materials("100-1", include_spares=True)
        assert {(row.part.part_num, row.color.id): row.quantity for row in with_spares}[
            ("3002", 4)
        ] == 2
        assert (await session.themes.children(1)) == ()
        assert await session.themes.parent(1) is None


@pytest.mark.asyncio
async def test_catalog_analytics_and_inventory_history(catalog_config: Config) -> None:
    async with await RebrickableSession.connect(catalog_config) as session:
        assert (await session.opened_catalog_state()).snapshot_id == "fixture-snapshot"
        assert (await session.current_catalog_state()).status is CatalogStatus.READY
        assert await session.parts.count() == 2
        assert [item.part_num async for item in session.parts.iter(page_size=1)] == [
            "3001",
            "3002",
        ]

        versions = await session.inventories.versions("100-1")
        assert [(item.version, item.is_latest) for item in versions] == [
            (1, False),
            (2, True),
        ]
        assert (await session.sets.inventory("100-1", version=1)).parts[
            0
        ].quantity == 99
        diff = await session.inventories.diff("100-1", 1, 2)
        assert diff.before_version == 1
        assert {(row.part.part_num, row.quantity_delta) for row in diff} == {
            ("3001", -97),
            ("3002", 0),
        }
        assert (
            next(
                row for row in diff if row.part.part_num == "3002"
            ).spare_quantity_delta
            == 1
        )

        occurrences = await session.parts.used_in_sets("3001")
        assert {(item.set.set_num, item.quantity) for item in occurrences} == {
            ("100-1", 2),
            ("200-1", 1),
        }
        usage = await session.parts.usage_stats("3001")
        assert (usage.total_quantity, usage.set_count, usage.color_count) == (3, 2, 2)
        relationships = await session.parts.relationships("3001")
        assert relationships[0].child_part_num == "3002"
        assert (await session.parts.relationships("3001", direction="children"))[
            0
        ].child_part_num == "3002"
        assert (await session.parts.relationships("3002", direction="parents"))[
            0
        ].parent_part_num == "3001"
        assert await session.parts.variants("3001") == relationships
        assert await session.parts.canonical_mold("3001") == "3001"
        assert (await session.elements.for_part_color("3001", 4))[
            0
        ].element_id == "3001004"
        assert [theme.id for theme in await session.themes.lineage(1)] == [1]
        assert (await session.minifigs.bill_of_materials("fig-1"))[0].quantity == 1
        unchanged = await session.inventories.diff("100-1", 2, 2)
        assert not unchanged
        assert unchanged[:] == ()

        with pytest.raises(ValueError, match="page_size"):
            await anext(session.parts.iter(page_size=0))
        with pytest.raises(ValueError, match="direction"):
            await session.parts.relationships("3001", direction="sideways")
        with pytest.raises(ValueError, match="max_depth"):
            await session.parts.canonical_mold("3001", max_depth=0)
        with pytest.raises(ValueError, match="limit"):
            await session.parts.used_in_sets("3001", limit=0)


@pytest.mark.asyncio
async def test_theme_tree_mold_cycle_and_filtered_occurrences(
    catalog_config: Config,
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    database = paths.database_for("fixture-snapshot")
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "INSERT INTO themes(id, name, parent_id) VALUES (?, ?, ?)",
            ((2, "Child", 1), (3, "Grandchild", 2)),
        )
        connection.execute("UPDATE sets SET theme_id=2 WHERE set_num='200-1'")
        connection.execute(
            "UPDATE search_documents SET theme_id=2 "
            "WHERE kind='set' AND canonical_id='200-1'"
        )
        connection.executemany(
            "INSERT INTO part_relationships VALUES (?, ?, ?)",
            (("M", "3001", "3002"), ("M", "3002", "3001")),
        )
        connection.commit()
    finally:
        connection.close()

    async with await RebrickableSession.open(catalog_config) as session:
        assert [item.id for item in await session.themes.children(1)] == [2]
        assert (await session.themes.parent(2)).id == 1
        assert [item.id for item in await session.themes.lineage(3)] == [1, 2, 3]
        assert [item.id for item in await session.themes.descendants(1)] == [2, 3]
        result = await session.search(
            "",
            kinds={SearchKind.SET},
            filters=SearchFilters(theme_id=2, include_subthemes=True),
        )
        assert [item.canonical_id for item in result.hits] == ["200-1"]
        assert await session.parts.canonical_mold("3001") == "3001"
        with pytest.raises(ValueError, match="depth"):
            await session.parts.canonical_mold("3001", max_depth=1)
        molds = await session.parts.relationships("3001", types={RelationshipType.MOLD})
        assert len(molds) == 2
        spares = await session.parts.used_in_sets(
            "3002", color_id=4, include_spares=True
        )
        assert [(item.set.set_num, item.is_spare) for item in spares] == [
            ("100-1", True)
        ]


@pytest.mark.asyncio
async def test_eager_connect_fails_for_missing_catalog(tmp_path: Path) -> None:
    config = Config(
        database_path=tmp_path / "missing.sqlite", cache_path=tmp_path / "cache"
    )
    with pytest.raises(CatalogUnavailableError):
        await RebrickableSession.connect(config)


@pytest.mark.asyncio
async def test_bom_validation_batches_catalog_evidence(catalog_config: Config) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        connection = await session._connection()
        statements: list[str] = []
        await connection.set_trace_callback(statements.append)
        report = await session.validate_bom(
            Bom.normalize(
                [
                    BomItem(
                        PartRef("rebrickable", "3001"), ColorRef("rebrickable", 4), 1
                    ),
                    BomItem(
                        PartRef("rebrickable", "3002"), ColorRef("rebrickable", 4), 1
                    ),
                ]
            )
        )
        await connection.set_trace_callback(None)
        assert report.unavailable_count == 0
        assert sum("COUNT(DISTINCT s.set_num)" in item for item in statements) == 1


@pytest.mark.asyncio
async def test_mapping_availability_and_validation(catalog_config: Config) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        part_match = await session.ldraw.resolve_ldraw_part("3001.dat")
        assert part_match.target_identifier == "3001"
        relationship = await session.ldraw.resolve_ldraw_part("not-present")
        assert relationship.target_identifier is None
        availability = await session.check_part_color(
            PartRef("ldraw", "3001"), ColorRef("ldraw", 4)
        )
        assert availability.available
        assert availability.element_ids == ("3001004",)
        report = await session.validate_bom(
            Bom.normalize([BomItem(PartRef("ldraw", "3001"), ColorRef("ldraw", 4), 2)]),
        )
        assert report.exact_count == 0
        assert report.unavailable_count == 0
        assert report.unique_count == len(report.rows) == 1
        assert report.rows[0].available
        assert report.rows[0].confidence == "user_override/user_override"


@pytest.mark.asyncio
async def test_invalid_pointer_is_unreadable(catalog_config: Config) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text("not-json")
    assert (await catalog_state(catalog_config)).status is CatalogStatus.UNREADABLE


@pytest.mark.asyncio
async def test_open_catalog_handles_hostile_paths(tmp_path: Path) -> None:
    config = build_catalog_config(tmp_path / "we#ird %dir")
    state = await catalog_state(config, verify=True)
    assert state.status is CatalogStatus.READY
    async with await RebrickableSession.open(config) as session:
        part = await session.parts.require("3001")
        assert part.name == "Brick 2 x 4"


@pytest.mark.asyncio
async def test_open_catalog_resolves_snapshot_while_holding_promotion_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    old_snapshot = paths.snapshots_dir / "fixture-snapshot"
    promoted_id = "promoted-snapshot"
    promoted_snapshot = paths.snapshots_dir / promoted_id
    shutil.copytree(old_snapshot, promoted_snapshot)
    acquired: list[bool] = []

    class PromotingLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def acquire(self) -> None:
            acquired.append(True)
            paths.active_pointer.write_text(json.dumps({"snapshot_id": promoted_id}))
            shutil.rmtree(old_snapshot)

        def release(self) -> None:
            pass

    monkeypatch.setattr("rebrickable.catalog.database.FileLock", PromotingLock)

    connection, state = await open_catalog(catalog_config)
    try:
        row = await (
            await connection.execute("SELECT name FROM parts LIMIT 1")
        ).fetchone()
    finally:
        await connection.close()

    assert acquired == [True]
    assert state.snapshot_id == promoted_id
    assert row is not None


@pytest.mark.asyncio
async def test_open_catalog_reports_lock_timeout(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_acquire(lock: object) -> None:
        raise Timeout(str(getattr(lock, "lock_file", "catalog")))

    monkeypatch.setattr("rebrickable.catalog.database.FileLock.acquire", fail_acquire)

    with pytest.raises(CatalogUnavailableError, match="catalog is busy"):
        await open_catalog(catalog_config)


@pytest.mark.asyncio
async def test_open_catalog_reports_corrupt_database(catalog_config: Config) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.database_for("fixture-snapshot").write_bytes(b"not sqlite")

    with pytest.raises(CatalogUnreadableError):
        await open_catalog(catalog_config)


@pytest.mark.asyncio
async def test_open_catalog_closes_connection_on_pragma_failure(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class FailingConnection:
        row_factory: object = None

        async def execute(self, _sql: str) -> None:
            raise sqlite3.OperationalError("query_only rejected")

        async def close(self) -> None:
            closed.append(True)

    state = await catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.READY

    async def fake_state(_config: Config, *, verify: bool = False) -> object:
        del verify
        return state

    def fake_connect(*_args: object, **_kwargs: object) -> object:
        async def _open() -> FailingConnection:
            return FailingConnection()

        return _open()

    monkeypatch.setattr("rebrickable.catalog.database.catalog_state", fake_state)
    monkeypatch.setattr("rebrickable.catalog.database.aiosqlite.connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError):
        await open_catalog(catalog_config)
    assert closed == [True]


@pytest.mark.asyncio
async def test_load_inventory_batches_element_queries(catalog_config: Config) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        connection = await session._connection()
        statements: list[str] = []
        await connection.set_trace_callback(statements.append)
        inventory = await session.sets.inventory("100-1")
        await connection.set_trace_callback(None)
        element_queries = [s for s in statements if "FROM elements" in s]
        assert len(element_queries) == 1
        by_key = {
            (item.part.part_num, item.color.id): item.element_ids
            for item in inventory.parts
        }
        assert by_key[("3001", 4)] == ("3001004",)
        assert by_key[("3002", 4)] == ("3002004",)


@pytest.mark.asyncio
async def test_concurrent_first_connection_opens_once(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_open = open_catalog

    async def counting_open(config: Config) -> object:
        calls.append(1)
        await asyncio.sleep(0)
        return await real_open(config)

    monkeypatch.setattr("rebrickable.session.open_catalog", counting_open)
    async with await RebrickableSession.open(catalog_config) as session:
        first, second = await asyncio.gather(
            session._connection(), session._connection()
        )
        assert first is second
    assert calls == [1]
