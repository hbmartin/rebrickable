# ruff: noqa: ANN202, ARG001
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rebrickable import Config, MappingSource, MappingStatus, RebrickableSession, cli
from rebrickable.bridge.cache import store_crosswalks
from rebrickable.bridge.models import (
    ColorMatch,
    PartMatch,
    TranslatedBomRow,
    TranslationReport,
)
from rebrickable.errors import (
    ApiError,
    CatalogUnavailableError,
    ConfigLoadError,
    RebrickableError,
)
from rebrickable.exports import to_json
from rebrickable.progress import ProgressEvent, ProgressStage
from rebrickable.types import RefreshOutcome, RefreshReport, SearchFilters, SearchKind


def use_config(monkeypatch, config: Config) -> None:
    monkeypatch.setattr(Config, "load", classmethod(lambda _cls, _path=None: config))


def translation_report(*, resolved: bool) -> TranslationReport:
    status = MappingStatus.RESOLVED if resolved else MappingStatus.UNRESOLVED
    part = PartMatch(
        "3001",
        "3001" if resolved else None,
        status,
        MappingSource.EXACT_CANONICAL_ID if resolved else MappingSource.NONE,
        1.0 if resolved else 0.0,
        (),
        "fixture",
        "fixture-snapshot",
    )
    color = ColorMatch(
        4,
        4 if resolved else None,
        status,
        MappingSource.USER_OVERRIDE if resolved else MappingSource.NONE,
        1.0 if resolved else 0.0,
        (),
        "fixture",
        "fixture-snapshot",
    )
    row = TranslatedBomRow(
        "3001",
        4,
        part.target_identifier,
        color.target_identifier,
        2,
        status,
        part,
        color,
    )
    return TranslationReport(
        (row,), int(resolved), 0, int(not resolved), "fixture-snapshot"
    )


def test_catalog_cli_commands_and_machine_streams(
    catalog_config: Config, monkeypatch, capsys, tmp_path: Path
) -> None:
    use_config(monkeypatch, catalog_config)
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "ready"

    assert cli.main(["search", "3001", "--kind", "part", "--limit", "1"]) == 0
    assert "3001" in capsys.readouterr().out
    assert cli.main(["search", "3001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "rebrickable.search"

    assert cli.main(["part", "3001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["part_num"] == "3001"
    assert cli.main(["set", "100-1", "--inventory"]) == 0
    assert "Inventory" in capsys.readouterr().out
    assert cli.main(["set", "100-1", "--bom", "--csv"]) == 0
    assert capsys.readouterr().out.startswith("part_num,color_id,quantity\r\n")
    assert cli.main(["set", "100-1", "--bom", "--json", "--include-spares"]) == 0
    bom_payload = json.loads(capsys.readouterr().out)
    assert bom_payload["schema"] == "rebrickable.entity"
    assert bom_payload["data"]["rows"][0]["quantity"] == 2
    assert bom_payload["data"]["skipped"] == []
    assert cli.main(["minifig", "fig-1", "--inventory", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["owner_num"] == "fig-1"
    assert cli.main(["set", "100-1", "--csv"]) == 2
    assert "--csv requires --bom" in capsys.readouterr().err
    assert cli.main(["part", "missing"]) == 3
    assert "not found" in capsys.readouterr().err

    output = tmp_path / "api.json"
    assert cli.main(["api-spec", "--output", str(output)]) == 0
    assert json.loads(output.read_text())["swagger"] == "2.0"


def test_refresh_translate_and_missing_status_cli(
    catalog_config: Config, monkeypatch, capsys, tmp_path: Path
) -> None:
    use_config(monkeypatch, catalog_config)

    async def fake_refresh(self, *, force=False, progress=None):
        if progress:
            progress(ProgressEvent(ProgressStage.DOWNLOAD, "parts", 10))
        return RefreshReport(
            RefreshOutcome.UPDATED,
            "new-snapshot",
            datetime(2026, 8, 1, tzinfo=UTC),
            (),
        )

    monkeypatch.setattr(RebrickableSession, "refresh_catalog", fake_refresh)
    assert cli.main(["refresh", "--force"]) == 0
    streams = capsys.readouterr()
    assert "new-snapshot" in streams.out
    assert "[download] parts 10" in streams.err
    assert cli.main(["refresh", "--json"]) == 0
    assert capsys.readouterr().err == ""

    reports = [translation_report(resolved=True), translation_report(resolved=False)]

    async def fake_translate(self, path: Path):
        del path
        return reports.pop(0)

    monkeypatch.setattr(
        "rebrickable.bridge.ldraw.LDrawBridge.translate_model_path", fake_translate
    )
    assert cli.main(["translate-ldraw", str(tmp_path / "model.ldr"), "--csv"]) == 0
    assert "ldraw_part_num" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "translate-ldraw",
                str(tmp_path / "model.ldr"),
                "--json",
                "--unresolved-only",
            ]
        )
        == 4
    )
    assert json.loads(capsys.readouterr().out)["data"][0]["status"] == "unresolved"

    missing = Config(
        database_path=tmp_path / "missing.sqlite", cache_path=tmp_path / "cache"
    )
    use_config(monkeypatch, missing)
    assert cli.main(["status"]) == 3


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (ValueError("bad"), 2),
        (CatalogUnavailableError("missing"), 3),
        (ApiError(401, "/path", "op", "no"), 5),
        (RebrickableError("library"), 1),
        (RuntimeError("boom"), 1),
    ],
)
def test_cli_error_boundaries(
    monkeypatch, capsys, error: Exception, exit_code: int
) -> None:
    async def fail(_args):
        raise error

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(["url", "part", "3001"]) == exit_code
    assert capsys.readouterr().err


def test_config_validation_load_write_failures_and_redaction(
    tmp_path: Path, monkeypatch
) -> None:
    for kwargs in (
        {"base_url": "http://bad"},
        {"downloads_base_url": "http://bad"},
        {"request_interval": -1},
        {"max_retries": -1},
        {"refresh_concurrency": 0},
        {"snapshot_retention": 0},
    ):
        with pytest.raises(ValueError):
            Config(**kwargs)

    path = tmp_path / "config.yml"
    path.write_text("\n")
    monkeypatch.delenv("REBRICKABLE_API_KEY", raising=False)
    assert Config.load(path).api_key is None
    path.write_text("database_path: 42\n")
    with pytest.raises(ConfigLoadError, match="path string"):
        Config.load(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigLoadError, match="unable to load"):
        Config.load(path)

    config = Config(
        api_key="do-not-export", mapping_overrides_path=tmp_path / "map.yml"
    )
    exported = to_json(config)
    assert "do-not-export" not in exported
    assert "api_key" not in exported
    assert str(tmp_path / "map.yml") in exported

    real_replace = os.replace

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("read only")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigLoadError, match="unable to write"):
        config.write(tmp_path / "failed.yml")
    monkeypatch.setattr(os, "replace", real_replace)
    assert not tuple(tmp_path.glob(".failed.yml.*"))


@pytest.mark.asyncio
async def test_search_validation_filters_browsing_and_escaping(
    catalog_config: Config,
) -> None:
    async with await RebrickableSession.open(catalog_config) as session:
        for kwargs in ({"limit": 0}, {"limit": 1001}, {"offset": -1}):
            with pytest.raises(ValueError):
                await session.search("brick", **kwargs)
        with pytest.raises(ValueError, match="empty"):
            await session.search("")
        browse = await session.search("", kinds={SearchKind.PART}, limit=1)
        assert browse.total == 2
        assert len(browse.hits) == 1
        filtered = await session.search(
            "",
            filters=SearchFilters(
                year_from=2020,
                year_to=2025,
                theme_id=1,
                min_parts=1,
                max_parts=10,
            ),
        )
        assert {hit.kind for hit in filtered.hits} == {SearchKind.SET}
        material = await session.search(
            "brick", filters=SearchFilters(category_id=1, material="Plastic")
        )
        assert material.total == 2
        assert (await session.search('3001" OR *', kinds={SearchKind.PART})).total == 0
        assert (await session.search("100%_", kinds={SearchKind.SET})).total == 0
        past_end = await session.search("", kinds={SearchKind.PART}, limit=1, offset=5)
        assert past_end.hits == ()
        assert past_end.total == 2


@pytest.mark.asyncio
async def test_search_labels_external_id_matches(catalog_config: Config) -> None:
    store_crosswalks(
        catalog_config,
        entity_kind="part",
        external_system="ldraw",
        canonical_id="3001",
        external_ids=("ld-3001",),
        operation_id="lego_parts_read",
        response_payload={"part_num": "3001"},
    )
    async with await RebrickableSession.open(catalog_config) as session:
        result = await session.search("ld-3001", kinds={SearchKind.PART})
        assert result.total == 1
        hit = result.hits[0]
        assert hit.canonical_id == "3001"
        assert hit.matched_field == "external_id"
        assert hit.matched_value == "ld-3001"
