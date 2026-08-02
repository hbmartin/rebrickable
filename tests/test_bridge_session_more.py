from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest

from rebrickable import (
    ColorRef,
    LDrawBomItem,
    LDrawColorInfo,
    MappingSource,
    MappingStatus,
    PartRef,
    RebrickableSession,
)
from rebrickable.api.models import ApiColor, ApiPart
from rebrickable.bridge.cache import store_crosswalks
from rebrickable.bridge.ldraw import _external_values
from rebrickable.catalog.database import CatalogPaths
from rebrickable.config import Config
from rebrickable.errors import OptionalDependencyError
from rebrickable.exports import (
    catalog_bom_to_csv,
    to_json,
    translation_table,
    translation_to_csv,
)


def database_for(config: Config) -> Path:
    paths = CatalogPaths.from_config(config)
    pointer = json.loads(paths.active_pointer.read_text())
    return paths.database_for(pointer["snapshot_id"])


@pytest.mark.asyncio
async def test_resolution_translation_substitutions_and_facades(
    catalog_config: Config,
) -> None:
    database = database_for(catalog_config)
    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT INTO api_crosswalk_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ("part", "ldraw", "cached-part", "3002", "op", "2026-08-01", "abc"),
            ("color", "ldraw", "7", "1", "op", "2026-08-01", "abc"),
        ),
    )
    connection.commit()
    connection.close()

    async with await RebrickableSession.open(catalog_config) as session:
        assert (await session.parts.list(limit=1))[0].part_num == "3001"
        with pytest.raises(ValueError):
            await session.parts.list(limit=0)
        assert (await session.colors.require(4)).name == "Red"
        assert (await session.categories.require(1)).name == "Bricks"
        assert (await session.elements.require("3001004")).design_id == "3001"
        assert (await session.minifigs.require("fig-1")).page_url.endswith("/fig-1/")
        assert (await session.sets.require("100-1")).page_url.endswith("/100-1/")
        assert (await session.themes.require(1)).page_url.endswith("/1/")

        overridden = await session.resolve_part(PartRef("ldraw", "3001"))
        assert overridden.source is MappingSource.USER_OVERRIDE
        cached = await session.resolve_part(PartRef("ldraw", "cached-part"))
        assert cached.target_identifier == "3002"
        canonical = await session.resolve_part(PartRef("rebrickable", "3002"))
        assert canonical.status is MappingStatus.RESOLVED
        absent = await session.resolve_part(PartRef("rebrickable", "absent"))
        assert absent.status is MappingStatus.UNRESOLVED
        external = await session.resolve_part(PartRef("bricklink", "absent"))
        assert external.status is MappingStatus.UNRESOLVED

        override_color = await session.resolve_color(ColorRef("ldraw", 4))
        assert override_color.target_identifier == 4
        cached_color = await session.resolve_color(ColorRef("ldraw", 7))
        assert cached_color.target_identifier == 1
        canonical_color = await session.resolve_color(ColorRef("rebrickable", 1))
        assert canonical_color.target_identifier == 1
        missing_color = await session.resolve_color(ColorRef("bricklink", 999))
        assert missing_color.status is MappingStatus.UNRESOLVED

        rgb = await session.ldraw.resolve_ldraw_color(
            LDrawColorInfo(99, "Anything", "#C91A09", 100)
        )
        assert rgb.target_identifier == 4
        name = await session.ldraw.resolve_ldraw_color(
            LDrawColorInfo(98, "Black", "FFFFFF", 255)
        )
        assert name.source is MappingSource.NAME
        unknown = await session.ldraw.resolve_ldraw_color(999)
        assert unknown.status is MappingStatus.UNRESOLVED
        assert await session.ldraw.find_ldraw_candidates("3002") == ("cached-part",)
        assert await session.ldraw.find_ldraw_color_candidates(1) == (7,)

        substitutions = await session.substitutes(PartRef("rebrickable", "3001"))
        assert substitutions[0].candidate.value == "3002"
        assert substitutions[0].path[0].value == "3001"
        with pytest.raises(ValueError, match="direction"):
            await session.substitutes(PartRef("rebrickable", "3001"), direction="side")

        identifiers = await session.external_ids(PartRef("rebrickable", "3002"))
        assert identifiers[0].value == "cached-part"
        report = await session.translate_bom(
            (
                LDrawBomItem("3001", 4, 2),
                LDrawBomItem("cached-part", 7, 1),
                LDrawBomItem("absent", 999, 1),
            )
        )
        assert report.resolved_count == 2
        assert report.unresolved_count == 1
        assert report.unresolved_rows[0].ldraw_part_num == "absent"
        assert not report.ambiguous_rows
        assert "cached-part" in translation_to_csv(report)
        assert "LDRAW PART" in translation_table(report)
        assert "schema_version" in to_json(report)
        bom_rows = await session.sets.bill_of_materials("100-1")
        assert catalog_bom_to_csv(bom_rows).startswith("part_num,color_id,quantity\r\n")


@pytest.mark.asyncio
async def test_override_lifecycle_optional_adapter_and_neutral_records(
    catalog_config: Config, monkeypatch
) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        await session.ldraw.record_part_override("custom.dat", "3002", reason="test")
        assert (
            await session.ldraw.resolve_ldraw_part("custom.dat")
        ).target_identifier == "3002"
        await session.ldraw.remove_part_override("custom.dat")
        assert (
            await session.ldraw.resolve_ldraw_part("custom.dat")
        ).status is MappingStatus.UNRESOLVED
        await session.ldraw.record_color_override(15, 1, reason="test")
        assert (await session.ldraw.resolve_ldraw_color(15)).target_identifier == 1
        await session.ldraw.remove_color_override(15)

        def missing_ldraw(_name: str) -> Never:
            raise ImportError("missing")

        monkeypatch.setattr(
            "rebrickable.bridge.ldraw.importlib.import_module", missing_ldraw
        )
        with pytest.raises(OptionalDependencyError):
            await session.ldraw.translate_model(object())

    @dataclass
    class Row:
        part: str = "3001"
        colour_code: int | None = 4
        quantity: int = 2

    assert LDrawBomItem.from_pyldraw_bom(Row()) == LDrawBomItem("3001", 4, 2)
    color_info = LDrawColorInfo.from_pyldraw_colour(
        type("Colour", (), {"code": 4, "name": "Red", "rgb": "C91A09", "alpha": None})()
    )
    assert color_info.alpha == 255
    with pytest.raises(ValueError):
        LDrawColorInfo.from_pyldraw_colour(object())
    with pytest.raises(ValueError):
        LDrawBomItem.from_pyldraw_bom(object())
    with pytest.raises(ValueError):
        LDrawBomItem.from_pyldraw_bom(Row(colour_code=None))
    with pytest.raises(ValueError):
        LDrawBomItem("3001", 4, 0)


def test_crosswalk_cache_updates_search_and_external_shape(
    catalog_config: Config,
) -> None:
    store_crosswalks(
        catalog_config,
        entity_kind="part",
        external_system="ldraw",
        canonical_id="3001",
        external_ids=("ld-3001",),
        operation_id="lego_parts_read",
        response_payload={"part_num": "3001"},
    )
    connection = sqlite3.connect(database_for(catalog_config))
    assert (
        connection.execute(
            "SELECT external_ids FROM search_documents WHERE kind='part' AND canonical_id='3001'"
        ).fetchone()[0]
        == "3001 ld-3001"
    )
    connection.close()
    assert _external_values({"LDraw": ["a", "a", "b"]}, "ldraw") == ("a", "b")
    assert _external_values({"LDraw": {"ext_ids": [1]}}, "ldraw") == ("1",)
    assert _external_values({"LDraw": "x"}, "ldraw") == ("x",)
    assert _external_values([], "ldraw") == ()


@pytest.mark.asyncio
async def test_explicit_api_enrichment(catalog_config: Config) -> None:
    class Client:
        async def get_part(self, part_num: str, **_query: object) -> ApiPart:
            return ApiPart(
                part_num=part_num,
                name="Brick",
                external_ids={"LDraw": ["ldraw-3001"]},
            )

        async def get_color(self, color_id: int) -> ApiColor:
            return ApiColor(
                id=color_id,
                name="Red",
                external_ids={"LDraw": {"ext_ids": [4]}},
            )

    async with await RebrickableSession.open(catalog_config) as session:
        assert await session.ldraw.enrich_part_mapping("3001", Client()) == (
            "ldraw-3001",
        )
        assert await session.ldraw.enrich_color_mapping(4, Client()) == ("4",)
