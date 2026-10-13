from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebrickable import CatalogState, Config
from rebrickable import refresh as module
from rebrickable.catalog.database import (
    CatalogPaths,
    _ActivePointer,
    _read_active_pointer,
)
from rebrickable.refresh import (
    _ACTIVATION_MARKER,
    _activation_history,
    _atomic_json,
    _managed_snapshot,
    _prune_snapshots,
    refresh_catalog,
)
from tests.test_refresh_state import downloader, snapshot_sources


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [1, 2, 3, 5])
async def test_retention_uses_activation_order_and_excludes_uncertain_directories(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, retain: int
) -> None:
    config = replace(catalog_config, snapshot_retention=retain)
    paths = CatalogPaths.from_config(config)
    (paths.snapshots_dir / "zzzz-legacy-unknown").mkdir()
    sources = snapshot_sources(config)
    stable = config.cache_path / "sources"
    stable.mkdir(parents=True)
    sources = {
        name: Path(shutil.copy2(path, stable / path.name))
        for name, path in sources.items()
    }
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    names = ["zz-first", "aa-second", "mm-third", "bb-fourth", "cc-fifth"]
    ids = iter(names)
    # The random suffix deliberately opposes chronological/name ordering.
    monkeypatch.setattr(
        "rebrickable.refresh.uuid",
        SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: SimpleNamespace(hex=next(ids))),
    )
    monkeypatch.setattr(
        "rebrickable.refresh.datetime",
        SimpleNamespace(now=lambda _zone: datetime(2026, 8, 1, tzinfo=UTC)),
    )
    generation = "a" * 32
    _atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": generation,
            "_activation_history": ["fixture-snapshot"],
        },
    )
    activated = ["fixture-snapshot"]
    for _ in names:
        report = await refresh_catalog(config)
        activated.append(report.snapshot_id)
        orphan = paths.snapshots_dir / "zzzz-managed-orphan"
        orphan.mkdir(exist_ok=True)
        _atomic_json(orphan / _ACTIVATION_MARKER, {"generation": generation})
    _prune_snapshots(
        paths,
        activated[-1],
        retain,
        previous_id=activated[-2],
        generation=generation,
        history=tuple(reversed(activated)),
    )
    assert {p.name for p in paths.snapshots_dir.iterdir()} == {
        *activated[-retain:],
        "zzzz-legacy-unknown",
    }
    pointer = json.loads(paths.active_pointer.read_text())
    assert pointer["_activation_generation"] == generation
    assert pointer["_activation_history"][0] == activated[-1]
    assert not tuple(config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "bad",
            "_activation_history": ["fixture-snapshot"],
        },
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot", "../outside"],
        },
        {
            "snapshot_id": "wrong",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot"],
        },
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot", "fixture-snapshot"],
        },
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot", []],
        },
    ],
)
def test_invalid_history_bootstraps_only_confirmed_active(
    catalog_config: Config, payload: object
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text(json.dumps(payload))
    pointer = _read_active_pointer(paths)
    generation, history = _activation_history(pointer)
    assert uuid.UUID(generation)
    assert history == ((pointer.snapshot_id,) if pointer.snapshot_id else ())


def test_missing_history_bootstraps_from_the_parsed_pointer(
    catalog_config: Config,
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.unlink()
    assert _activation_history(_read_active_pointer(paths))[1] == ()
    _atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot"],
        },
    )
    assert _activation_history(_read_active_pointer(paths))[1] == ("fixture-snapshot",)


def test_prune_retries_failed_history_and_protects_changed_pointer(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = paths.snapshots_dir / "older-activated"
    previous.mkdir()
    original = shutil.rmtree
    attempts = []

    def fail_once(path: Path) -> None:
        attempts.append(path)
        raise OSError("busy snapshot")

    monkeypatch.setattr("rebrickable.refresh.shutil.rmtree", fail_once)
    history = ("fixture-snapshot", previous.name)
    _prune_snapshots(paths, "fixture-snapshot", 1, history=history)
    assert attempts == [previous]
    monkeypatch.setattr("rebrickable.refresh.shutil.rmtree", original)
    _prune_snapshots(paths, "wrong-active", 1, history=history)
    assert previous.is_dir()
    _prune_snapshots(paths, "fixture-snapshot", 1, history=history)
    assert not previous.exists()


def test_markers_and_missing_snapshot_directory(
    catalog_config: Config, tmp_path: Path
) -> None:
    marker = tmp_path / _ACTIVATION_MARKER
    assert not _managed_snapshot(tmp_path, None)
    assert not _managed_snapshot(tmp_path, "generation")
    marker.write_text("invalid JSON")
    assert not _managed_snapshot(tmp_path, "generation")
    _atomic_json(marker, {"generation": "another-generation"})
    assert not _managed_snapshot(tmp_path, "generation")
    paths = CatalogPaths.from_config(catalog_config)
    missing = replace(paths, snapshots_dir=tmp_path / "missing")
    _prune_snapshots(missing, "fixture-snapshot", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_promotion_verifies_only_changed_active_snapshot(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, *, changed: bool
) -> None:
    original = module._catalog_state
    original_initial = module.catalog_state
    original_pointer = module._read_active_pointer
    calls = []
    sources = snapshot_sources(catalog_config)
    paths = CatalogPaths.from_config(catalog_config)
    if changed:
        shutil.copytree(
            paths.snapshots_dir / "fixture-snapshot",
            paths.snapshots_dir / "replacement",
        )

    async def initial(config, *, verify=False) -> CatalogState:
        calls.append(verify)
        return await original_initial(config, verify=verify)

    def pointer(paths) -> _ActivePointer:
        if changed:
            _atomic_json(paths.active_pointer, {"snapshot_id": "replacement"})
        return original_pointer(paths)

    async def counted(config, pointer, *, verify=False) -> CatalogState:
        calls.append(verify)
        return await original(config, pointer, verify=verify)

    monkeypatch.setattr(module, "catalog_state", initial)
    monkeypatch.setattr(module, "_catalog_state", counted)
    monkeypatch.setattr(module, "_read_active_pointer", pointer)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    await refresh_catalog(catalog_config)
    assert calls == ([True, False, True] if changed else [True, False])


@pytest.mark.asyncio
async def test_corrupted_history_starts_new_generation_and_preserves_uncertainty(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(catalog_config, snapshot_retention=3)
    paths = CatalogPaths.from_config(config)
    monkeypatch.setattr(module, "_download_one", downloader(snapshot_sources(config)))
    first = await refresh_catalog(config)
    pointer = json.loads(paths.active_pointer.read_text())
    old_generation = pointer["_activation_generation"]
    orphan = paths.snapshots_dir / "old-generation-orphan"
    orphan.mkdir()
    _atomic_json(orphan / _ACTIVATION_MARKER, {"generation": old_generation})
    pointer["_activation_history"] = ["../untrusted"]
    _atomic_json(paths.active_pointer, pointer)
    report = await refresh_catalog(replace(config, snapshot_retention=1))
    pointer = json.loads(paths.active_pointer.read_text())
    assert pointer["_activation_generation"] != old_generation
    assert pointer["_activation_history"] == [report.snapshot_id, first.snapshot_id]
    assert {path.name for path in paths.snapshots_dir.iterdir()} == {
        report.snapshot_id,
        "fixture-snapshot",
        orphan.name,
    }


@pytest.mark.asyncio
async def test_failed_deletion_stays_in_history_for_next_promotion(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(catalog_config, snapshot_retention=1)
    paths = CatalogPaths.from_config(config)
    sources = snapshot_sources(config)
    stable = config.cache_path / "sources"
    stable.mkdir(parents=True)
    sources = {
        name: Path(shutil.copy2(path, stable / path.name))
        for name, path in sources.items()
    }
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    original = shutil.rmtree

    def fail_previous(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == paths.snapshots_dir / "fixture-snapshot":
            raise OSError("busy snapshot")
        original(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", fail_previous)
    first = await refresh_catalog(config)
    pointer = json.loads(paths.active_pointer.read_text())
    assert pointer["_activation_history"] == [first.snapshot_id, "fixture-snapshot"]
    assert paths.database_for("fixture-snapshot").is_file()
    monkeypatch.setattr(module.shutil, "rmtree", original)
    report = await refresh_catalog(config)
    assert {path.name for path in paths.snapshots_dir.iterdir()} == {report.snapshot_id}
