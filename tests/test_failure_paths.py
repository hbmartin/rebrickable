from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from rebrickable import Bom, Config, PartRef, RebrickableSession
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.errors import ConfigLoadError
from rebrickable.refresh import _prune_abandoned_staging, _prune_snapshots


@pytest.mark.parametrize("field", ["max_retry_after", "snapshot_retention"])
def test_invalid_operational_bounds(catalog_config: Config, field: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        replace(catalog_config, **{field: 0})


def test_config_null_and_non_string_path(tmp_path: Path) -> None:
    target = tmp_path / "config.yml"
    target.write_text("null")
    assert Config.load(target).api_key is None
    target.write_text("database_path: 17")
    with pytest.raises(ConfigLoadError, match="path string"):
        Config.load(target)


async def test_manifest_missing_database_and_naive_date(catalog_config: Config) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    manifest = paths.manifest_for("fixture-snapshot")
    payload = json.loads(manifest.read_text())
    payload["retrieved_at"] = "2026-08-01T00:00:00"
    manifest.write_text(json.dumps(payload))
    state = await catalog_state(catalog_config)
    assert state.retrieved_at is not None
    assert state.retrieved_at.tzinfo is not None
    paths.database_for("fixture-snapshot").unlink()
    assert (await catalog_state(catalog_config)).diagnostics == (
        "snapshot database is missing",
    )


@pytest.mark.parametrize("row", [None, ("corrupt",)])
async def test_quick_check_failure(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, row: object
) -> None:
    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def execute(self, _sql: str) -> Connection:
            return self

        async def fetchone(self) -> object:
            return row

    monkeypatch.setattr(
        "rebrickable.catalog.database.aiosqlite.connect",
        lambda *_args, **_kwargs: Connection(),
    )
    assert (await catalog_state(catalog_config, verify=True)).diagnostics == (
        "SQLite quick_check failed",
    )


def test_staging_prune_preserves_recent_and_unrelated_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "refresh-old"
    recent = tmp_path / "refresh-recent"
    unrelated = tmp_path / "other"
    for directory in (old, recent, unrelated):
        directory.mkdir()
    os.utime(old, (0, 0))
    _prune_abandoned_staging(tmp_path)
    assert not old.exists()
    assert recent.is_dir()
    assert unrelated.is_dir()
    _prune_abandoned_staging(tmp_path / "missing")
    old.mkdir()
    os.utime(old, (0, 0))

    def denied(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr("rebrickable.refresh.shutil.rmtree", denied)
    _prune_abandoned_staging(tmp_path)
    assert old.is_dir()


def test_snapshot_prune_ignores_cleanup_failure(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    orphan = paths.snapshots_dir / "orphan"
    orphan.mkdir()

    def denied(_path: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr("rebrickable.refresh.shutil.rmtree", denied)
    _prune_snapshots(paths, "fixture-snapshot", 1)
    assert orphan.is_dir()
    assert paths.database_for("fixture-snapshot").is_file()
    _prune_snapshots(
        replace(paths, snapshots_dir=paths.snapshots_dir / "missing"), "none", 1
    )


async def test_substitution_bounds_and_empty_validation(catalog_config: Config) -> None:
    async with await RebrickableSession.connect(catalog_config) as session:
        for kwargs in ({"max_depth": -1}, {"max_candidates": -1}):
            with pytest.raises(ValueError, match="negative"):
                await session.substitutes(PartRef("rebrickable", "3001"), **kwargs)
        assert await session.substitutes(PartRef("rebrickable", "missing")) == ()
        assert (
            await session.substitutes(PartRef("rebrickable", "3001"), max_depth=0) == ()
        )
        assert (
            len(
                await session.substitutes(
                    PartRef("rebrickable", "3001"),
                    direction="children",
                    max_candidates=1,
                )
            )
            == 1
        )
        assert (
            len(
                await session.substitutes(
                    PartRef("rebrickable", "3002"),
                    direction="parents",
                    max_candidates=1,
                )
            )
            == 1
        )
        assert (
            await session.substitutes(
                PartRef("rebrickable", "3001"), include_mold_variants=False
            )
            != ()
        )
        assert (await session.validate_bom(Bom.normalize([]))).rows == ()


def test_cli_final_exception_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebrickable import cli  # noqa: PLC0415 - patch target

    async def fail(_args: object) -> int:
        raise RuntimeError("unexpected fixture failure")

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(["url", "part", "3001"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unexpected fixture failure" in captured.err


def test_integer_subclass_normalization_is_preserved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from rebrickable import ColorRef, cli  # noqa: PLC0415 - public contracts

    color = ColorRef("rebrickable", True)
    assert color.value == 1
    assert type(color.value) is int
    status = cli.main(["url", "part", "3001"])
    assert status == 0
    assert type(status) is int
    assert capsys.readouterr().err == ""
