# ruff: noqa: ANN202, ARG001, ARG002, PT018
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import ldraw
import pytest
from pydantic import ValidationError

from rebrickable import (
    Bom,
    BomItem,
    ColorRef,
    Config,
    LDrawColorInfo,
    MappingStatus,
    PartRef,
    RebrickableClient,
    RebrickableSession,
)
from rebrickable.api.models import ApiMinifig, ApiRecord
from rebrickable.bridge.cache import store_crosswalks
from rebrickable.bridge.ldraw import _external_values
from rebrickable.bridge.overrides import read_overrides, write_overrides
from rebrickable.catalog.database import CatalogPaths, open_catalog
from rebrickable.catalog.importers import (
    DATASET_BY_NAME,
    _batch,
    _boolean,
    _nullable_integer,
    _quantity,
    _rgb,
    _rows,
    import_catalog,
    inspect_header,
    validate_references,
)
from rebrickable.errors import (
    ApiError,
    CatalogImportError,
    CatalogSchemaError,
    CatalogUnavailableError,
    ConfigLoadError,
    DatasetIntegrityError,
    DatasetSchemaError,
    InventoryCycleError,
    InventoryNotFoundError,
)
from rebrickable.exports import translation_to_csv
from rebrickable.refresh import _carry_crosswalk

from .conftest import write_dataset
from .test_api import FakeResponse, FakeTransport
from .test_bridge_session_more import database_for


@pytest.mark.asyncio
async def test_session_edge_resolution_availability_refresh_and_cycles(
    catalog_config: Config, monkeypatch
) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        direct = await session.check_part_color(
            PartRef("rebrickable", "3001"), ColorRef("rebrickable", 4)
        )
        assert direct.confidence == "exact/exact"
        unsupported = await session.check_part_color(
            PartRef("lego", "3001"), ColorRef("bricklink", 4)
        )
        assert not unsupported.available
        relationship_only = await session.check_part_color(
            PartRef("rebrickable", "3002"), ColorRef("rebrickable", 1)
        )
        assert not relationship_only.available
        assert relationship_only.relationship_only
        validation = await session.validate_bom(
            Bom.normalize(
                [
                    BomItem(
                        PartRef("rebrickable", "3002"),
                        ColorRef("rebrickable", 1),
                        1,
                    )
                ]
            )
        )
        assert validation.substitutable_count == 1
        assert validation.rows[0].substitutions[0].candidate.value == "3001"
        assert await session.external_ids(PartRef("bricklink", "missing")) == ()
        assert await session.substitutes(PartRef("rebrickable", "missing")) == ()
        assert await session.ldraw.resolve_rebrickable_part("3001")
        reverse = await session.ldraw.resolve_rebrickable_part("3002")
        assert reverse.status is MappingStatus.AMBIGUOUS
        assert reverse.target_identifier is None
        assert reverse.candidates[0].identifier == "3002"
        assert reverse.candidates[0].confidence == 0.5
        assert await session.ldraw.find_ldraw_candidates("3002") == ("3002",)
        assert (
            await session.ldraw.resolve_rebrickable_part("missing")
        ).status is MappingStatus.UNRESOLVED

    calls: list[bool] = []

    async def fake_refresh(config, *, force=False, callback=None):
        calls.append(force)
        return SimpleNamespace(snapshot_id="x")

    monkeypatch.setattr("rebrickable.session.refresh_catalog", fake_refresh)
    async with await RebrickableSession.open(catalog_config) as session:
        report = await session.refresh_catalog(force=True)
        assert report.snapshot_id == "x" and calls == [True]

    database = database_for(catalog_config)
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO inventory_sets VALUES (3, '100-1', 1)")
    connection.execute("INSERT INTO inventory_parts VALUES (2, '3001', -1, 1, 0, NULL)")
    connection.commit()
    connection.close()
    async with await RebrickableSession.open(catalog_config) as session:
        inventory = await session.sets.inventory("100-1")
        assert any(item.color.name == "Unknown" for item in inventory.parts)
        with pytest.raises(InventoryNotFoundError):
            await session.sets.inventory("missing")
        with pytest.raises(InventoryCycleError) as captured:
            await session.sets.bill_of_materials("100-1")
        assert captured.value.path == ("100-1", "200-1", "100-1")


@pytest.mark.asyncio
async def test_bom_skips_missing_sub_inventories(catalog_config: Config) -> None:
    database = database_for(catalog_config)
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM inventories WHERE owner_num='fig-1'")
    connection.commit()
    connection.close()
    async with await RebrickableSession.open(catalog_config) as session:
        bom = await session.sets.bill_of_materials("100-1")
        quantities = {(row.part.part_num, row.color.id): row.quantity for row in bom}
        assert quantities == {("3001", 4): 2, ("3001", 1): 2}
        assert len(bom.skipped) == 1
        skipped = bom.skipped[0]
        assert skipped.owner_num == "fig-1"
        assert skipped.owner_path == ("100-1", "fig-1")
        assert "fig-1" in skipped.reason
        with pytest.raises(InventoryNotFoundError):
            await session.sets.bill_of_materials("100-1", strict=True)
        with pytest.raises(InventoryNotFoundError):
            await session.sets.bill_of_materials("missing")


@pytest.mark.asyncio
async def test_mapping_ambiguity_relationship_and_optional_ldraw_success(
    catalog_config: Config, tmp_path: Path
) -> None:
    database = database_for(catalog_config)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO colors VALUES (5, 'Other Red', 'C91A09', 1, 0, 0, NULL, NULL)"
    )
    connection.executemany(
        "INSERT INTO api_crosswalk_cache VALUES ('color','ldraw',?, '1','op','date','hash')",
        (("7",), ("8",)),
    )
    # A dangling relationship endpoint is retained as a candidate strategy.
    connection.execute("DELETE FROM parts WHERE part_num='3002'")
    connection.commit()
    connection.close()

    async with await RebrickableSession.open(catalog_config) as session:
        relationship = await session.ldraw.resolve_ldraw_part("3002")
        assert relationship.status is MappingStatus.AMBIGUOUS
        ambiguous = await session.ldraw.resolve_ldraw_color(
            LDrawColorInfo(90, "No name", "C91A09", 10)
        )
        assert ambiguous.status is MappingStatus.AMBIGUOUS
        absent = await session.ldraw.resolve_ldraw_color(
            LDrawColorInfo(91, "No name", "FFFFFF", 10)
        )
        assert absent.status is MappingStatus.UNRESOLVED
        reverse = await session.ldraw.resolve_rebrickable_color(1)
        assert reverse.status is MappingStatus.AMBIGUOUS
        assert await session.ldraw.find_ldraw_color_candidates(1) == (7, 8)
        report = await session.ldraw.translate_bom(
            (LDrawBomItemFixture("3002", 90, 1),),
            colors={90: LDrawColorInfo(90, "No name", "C91A09", 10)},
        )
        assert report.ambiguous_count == 1
        assert translation_to_csv(report, unresolved_only=True).count("\r\n") == 2

        library = tmp_path / "ldraw"
        parts_directory = library / "parts"
        parts_directory.mkdir(parents=True)
        parts_list = library / "parts.lst"
        parts_list.write_text("3001.DAT Brick 2 x 4\n", encoding="utf-8")
        (library / "LDConfig.ldr").write_text(
            "0 !COLOUR Black CODE 99 VALUE #05131D EDGE #333333\n",
            encoding="utf-8",
        )
        (parts_directory / "3001.dat").write_text(
            "0 Brick 2 x 4\n0 Name: 3001.dat\n0 !LDRAW_ORG Part\n0 BFC CERTIFY CCW\n",
            encoding="utf-8",
        )
        model_path = tmp_path / "model.ldr"
        model_path.write_text(
            "0 Local fixture\n1 99 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n",
            encoding="utf-8",
        )
        plain_path = tmp_path / "plain.ldr"
        plain_path.write_text(
            "0 Local fixture without library metadata\n"
            "1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n",
            encoding="utf-8",
        )
        partial_path = tmp_path / "partial.ldr"
        partial_path.write_text(
            "0 Tolerant fixture\n"
            "this is invalid\n"
            "1 99 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n",
            encoding="utf-8",
        )

        # pyldraw3 reads only these local fixture files; it performs no downloads.
        parts = ldraw.Parts.get(parts_list)
        loaded = ldraw.load_model(model_path, parts=parts)
        assert loaded.model is not None
        assert loaded.complete
        assert loaded.diagnostics == ()
        with_parts = await session.ldraw.translate_model(loaded.model, parts=parts)
        assert with_parts.resolved_count == 1
        assert with_parts.rows[0].rebrickable_color_id == 1
        plain = await session.ldraw.translate_model_path(plain_path)
        assert plain.resolved_count == 1
        assert plain.diagnostics == ()
        via_library = await session.ldraw.translate_model_path(
            model_path, library_path=parts_list
        )
        assert via_library.rows[0].rebrickable_color_id == 1
        assert (
            await session.ldraw.annotate_pyldraw_bom(
                [ldraw.BomRow("3001", "Brick 2 x 4", 4, "Red", 1)]
            )
        ).resolved_count == 1
        with_colors = await session.ldraw.annotate_pyldraw_bom(
            [ldraw.BomRow("3001", "Brick 2 x 4", 99, "Black", 1)],
            parts=parts,
        )
        assert with_colors.rows[0].rebrickable_color_id == 1
        partial = await session.ldraw.translate_model_path(
            partial_path, library_path=parts_list
        )
        assert "Unknown command (this)" in partial.diagnostics[0]
        assert partial.diagnostics[-1] == "model source is incomplete"
        with pytest.raises(ValueError, match="could not read model"):
            await session.ldraw.translate_model_path(
                tmp_path / "missing.ldr", library_path=parts_list
            )


@dataclass(frozen=True)
class LDrawBomItemFixture:
    part_num: str
    color_code: int
    quantity: int


@pytest.mark.asyncio
async def test_override_configuration_and_external_value_edges(tmp_path: Path) -> None:
    config = Config(
        database_path=tmp_path / "missing.sqlite",
        cache_path=tmp_path / "cache",
        mapping_overrides_path=tmp_path / "maps.yml",
    )
    async with await RebrickableSession.open(config) as session:
        await session.ldraw.record_part_override("x", "3001")
        await session.ldraw.remove_part_override("x")
        paths = CatalogPaths.from_config(config)
        paths.active_pointer.write_text(json.dumps({"snapshot_id": None}))
        await session.ldraw.record_part_override("invalid-pointer", "3001")

    async with await RebrickableSession.open(
        Config(database_path=tmp_path / "other.sqlite", cache_path=tmp_path / "cache2")
    ) as no_path:
        with pytest.raises(ValueError, match="not configured"):
            await no_path.ldraw.record_part_override("x", "3001")
        with pytest.raises(ValueError, match="not configured"):
            await no_path.ldraw.remove_part_override("x")

    assert _external_values({"Other": [1]}, "ldraw") == ()
    assert _external_values({"LDraw": {"ids": [2]}}, "ldraw") == ("2",)
    assert _external_values({"LDraw": object()}, "ldraw") == ()


def test_override_file_validation_and_atomic_failure(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "maps.yml"
    for payload in (
        "- bad",
        "version: 2",
        "version: 1\nparts: bad",
        "version: 1\nparts: [bad]",
        "version: 1\nparts: [{source_id: ''}]",
        "version: 1\nparts: [{source_id: '.dat', target_id: '3001'}]",
        "version: 1\ncolors: [{source_id: x, target_id: '4'}]",
        "version: 1\ncolors: [{source_id: '4', target_id: xyz}]",
    ):
        path.write_text(payload)
        with pytest.raises(ConfigLoadError):
            read_overrides(path)
    path.write_text("\n")
    assert read_overrides(path) == ()
    path.write_text("version: 1\nparts: [{source_id: 3001.DAT, target_id: '3001'}]")
    assert read_overrides(path)[0]["source_id"] == "3001"
    path.write_text(
        "version: 1\n"
        "parts:\n"
        "  - {source_id: 3001.dat, target_id: '3001'}\n"
        "  - {source_id: PARTS/3001.DAT, target_id: '3002'}\n"
    )
    with pytest.raises(ConfigLoadError, match="duplicate mapping override"):
        read_overrides(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigLoadError):
        read_overrides(path)

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("no")

    monkeypatch.setattr("rebrickable._atomic._replace", fail_replace)
    with pytest.raises(OSError):
        write_overrides(path, ())
    assert not tuple(tmp_path.glob(".maps.yml.*"))


def test_crosswalk_and_carry_failure_edges(
    tmp_path: Path, catalog_config: Config
) -> None:
    missing = Config(
        database_path=tmp_path / "missing.sqlite", cache_path=tmp_path / "cache"
    )
    with pytest.raises(CatalogUnavailableError):
        store_crosswalks(
            missing,
            entity_kind="part",
            external_system="ldraw",
            canonical_id="3001",
            external_ids=("x",),
            operation_id="op",
            response_payload={},
        )
    with pytest.raises(CatalogImportError):
        _carry_crosswalk(None, tmp_path / "not-a-database.sqlite")

    paths = CatalogPaths.from_config(catalog_config)
    store_crosswalks(
        catalog_config,
        entity_kind="part",
        external_system="ldraw",
        canonical_id="3001",
        external_ids=(),
        operation_id="lego_parts_read",
        response_payload={},
    )
    paths.active_pointer.write_text(json.dumps({"snapshot_id": "missing-snapshot"}))
    with pytest.raises(CatalogUnavailableError):
        store_crosswalks(
            catalog_config,
            entity_kind="part",
            external_system="ldraw",
            canonical_id="3001",
            external_ids=("x",),
            operation_id="lego_parts_read",
            response_payload={},
        )


def test_crosswalk_resolves_snapshot_after_acquiring_promotion_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    old_snapshot = paths.snapshots_dir / "fixture-snapshot"
    promoted_id = "promoted-snapshot"
    promoted_snapshot = paths.snapshots_dir / promoted_id
    shutil.copytree(old_snapshot, promoted_snapshot)

    class PromotingLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> PromotingLock:
            paths.active_pointer.write_text(json.dumps({"snapshot_id": promoted_id}))
            shutil.rmtree(old_snapshot)
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr("rebrickable.bridge.cache.FileLock", PromotingLock)

    store_crosswalks(
        catalog_config,
        entity_kind="part",
        external_system="ldraw",
        canonical_id="3001",
        external_ids=("race-safe",),
        operation_id="lego_parts_read",
        response_payload={"part_num": "3001"},
    )

    with closing(sqlite3.connect(paths.database_for(promoted_id))) as connection:
        row = connection.execute(
            "SELECT canonical_id FROM api_crosswalk_cache WHERE external_id=?",
            ("race-safe",),
        ).fetchone()
    assert row == ("3001",)


def test_importer_parser_batches_and_failures(
    tmp_path: Path, catalog_config: Config
) -> None:
    assert _boolean("TRUE") == 1
    assert _boolean("false") == 0
    assert _boolean("t") == 1
    assert _boolean("F") == 0
    with pytest.raises(ValueError):
        _boolean("yes")
    assert _nullable_integer("") is None
    assert _nullable_integer("2") == 2
    with pytest.raises(ValueError):
        _quantity("-1")
    with pytest.raises(ValueError):
        _rgb("red")
    batches = list(_batch(iter([(i,) for i in range(5001)])))
    assert [len(batch) for batch in batches] == [5000, 1]

    empty = tmp_path / "empty.csv.gz"
    with gzip.open(empty, "wt"):
        pass
    with pytest.raises(DatasetSchemaError):
        inspect_header(empty, DATASET_BY_NAME["parts"])
    duplicate = tmp_path / "duplicate-parts.csv.gz"
    with gzip.open(duplicate, "wt") as handle:
        handle.write("part_num,name,part_cat_id,material,name\n")
        handle.write("3001,Brick,1,Plastic,Duplicate\n")
    with pytest.raises(DatasetSchemaError, match="duplicate columns: name"):
        inspect_header(duplicate, DATASET_BY_NAME["parts"])
    invalid = write_dataset(
        tmp_path, "colors", [(1, "Red", "BAD", "True", 1, 1, "", "")]
    )
    with pytest.raises(DatasetSchemaError, match=":2"):
        tuple(_rows(invalid, DATASET_BY_NAME["colors"]))

    database = database_for(catalog_config)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO inventory_parts VALUES (99999, 'missing', 4, 1, 0, NULL)"
    )
    connection.commit()
    with pytest.raises(DatasetIntegrityError, match="cross-file"):
        validate_references(connection)
    connection.close()

    sources: dict[str, Path] = {}
    for path in database.parent.glob("*.csv.gz"):
        sources[path.name.removesuffix(".csv.gz")] = path
    copied = tmp_path / "copy.sqlite"
    import_catalog(sources, copied, snapshot_id="one", retrieved_at="date")
    with pytest.raises(CatalogImportError):
        import_catalog(sources, copied, snapshot_id="two", retrieved_at="date")


@pytest.mark.asyncio
async def test_open_catalog_error_classes(catalog_config: Config) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = json.loads(paths.active_pointer.read_text())
    manifest = paths.manifest_for(pointer["snapshot_id"])
    payload = json.loads(manifest.read_text())
    payload["schema_version"] = 999
    manifest.write_text(json.dumps(payload))
    with pytest.raises(CatalogSchemaError):
        await open_catalog(catalog_config)
    paths.active_pointer.unlink()
    with pytest.raises(CatalogUnavailableError):
        await open_catalog(catalog_config)


@pytest.mark.asyncio
async def test_client_remaining_pacing_retry_close_and_models(monkeypatch) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("rebrickable.api.client.asyncio.sleep", record_sleep)
    api = RebrickableClient(
        api_key="key", config=Config(request_interval=0), transport=FakeTransport()
    )
    api._next_request_at = asyncio.get_running_loop().time() + 0.01
    await api._pace()
    assert sleeps
    assert (
        RebrickableClient._detail(FakeResponse({"message": "no detail"}))
        == "upstream request failed"
    )

    broken = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=0),
        transport=RaisingTransport(),
    )
    with pytest.raises(ApiError, match="transport failure"):
        await broken.get_part("3001")

    throttled = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=1),
        transport=FakeTransport(
            FakeResponse({"detail": "after 0 seconds"}, status_code=429),
            FakeResponse({"part_num": "3001", "name": "Brick"}),
        ),
    )
    assert (await throttled.get_part("3001")).part_num == "3001"

    single_page = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0),
        transport=FakeTransport(
            FakeResponse({"count": 0, "next": None, "previous": None, "results": []})
        ),
    )
    assert [item async for item in single_page.iter_colors()] == []
    assert ApiMinifig(set_num="fig-1", name="Fig").fig_num == "fig-1"
    with pytest.raises(ValidationError):
        ApiRecord.model_validate("bad")

    owned = RebrickableClient(api_key="key")
    closed: list[bool] = []

    async def fake_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr("rebrickable.api.transport.HttpxTransport.close", fake_close)
    await owned.close()
    assert closed == [True]


class RaisingTransport:
    async def request(self, method, url, *, headers, params=None, data=None, json=None):
        raise OSError("offline")
