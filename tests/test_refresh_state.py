# ruff: noqa: ANN002, ANN003, ANN202, ANN204, ARG001, PT018
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from filelock import FileLock, Timeout

from rebrickable import CatalogStatus, Config, RebrickableSession, RefreshOutcome
from rebrickable import refresh as refresh_module
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.errors import CatalogImportError, DatasetIntegrityError, DownloadError
from rebrickable.progress import ProgressEvent, ProgressStage
from rebrickable.refresh import (
    _atomic_json,
    _download_one,
    _prune_abandoned_staging,
    _prune_snapshots,
    refresh_catalog,
)


def snapshot_sources(config: Config) -> dict[str, Path]:
    paths = CatalogPaths.from_config(config)
    pointer = json.loads(paths.active_pointer.read_text())
    snapshot = paths.snapshots_dir / pointer["snapshot_id"]
    return {
        path.name.removesuffix(".csv.gz"): path for path in snapshot.glob("*.csv.gz")
    }


def downloader(sources: dict[str, Path], *, unchanged: bool = False):
    async def fake(
        _client,
        *,
        url: str,
        destination: Path,
        previous,
        force: bool,
        callback,
    ) -> tuple[bool, dict[str, Any]]:
        del force, callback
        if unchanged and previous is not None:
            return False, asdict(previous)
        source = sources[destination.name.removesuffix(".csv.gz")]
        shutil.copy2(source, destination)
        return True, {
            "url": url,
            "etag": '"fixture"',
            "last_modified": "Sat, 01 Aug 2026 00:00:00 GMT",
            "byte_length": destination.stat().st_size,  # noqa: ASYNC240 - small local fixture; no production I/O
            "sha256": "a" * 64,
        }

    return fake


@pytest.mark.asyncio
async def test_complete_refresh_promotion_unchanged_and_progress(
    catalog_config: Config, tmp_path: Path, monkeypatch
) -> None:
    sources = snapshot_sources(catalog_config)
    config = Config(
        database_path=tmp_path / "new" / "catalog.sqlite",
        cache_path=tmp_path / "cache",
        mapping_overrides_path=catalog_config.mapping_overrides_path,
    )
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    events: list[ProgressEvent] = []
    report = await refresh_catalog(config, callback=events.append)
    assert report.outcome is RefreshOutcome.UPDATED
    assert len(report.files) == 12
    assert events[-1].stage is ProgressStage.DONE
    paths = CatalogPaths.from_config(config)
    assert paths.database_for(report.snapshot_id).is_file()
    assert (paths.snapshots_dir / report.snapshot_id / "parts.csv.gz").is_file()
    with FileLock(paths.lock_file, timeout=0.1):
        pass
    async with await RebrickableSession.open(config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"

    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(sources, unchanged=True)
    )
    unchanged = await refresh_catalog(config)
    assert unchanged.outcome is RefreshOutcome.UNCHANGED
    assert unchanged.snapshot_id == report.snapshot_id


@pytest.mark.asyncio
async def test_integrity_retry_and_failed_promotion_preserve_pointer(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_text()
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    attempts = 0

    def fail_import(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise DatasetIntegrityError(f"mixed generation (attempt {attempts})")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("rebrickable.refresh.import_catalog", fail_import)
    monkeypatch.setattr("rebrickable.refresh.asyncio.sleep", no_sleep)
    with pytest.raises(DatasetIntegrityError, match="attempt 2") as excinfo:
        await refresh_catalog(catalog_config)
    assert attempts == 2
    assert isinstance(excinfo.value.__cause__, DatasetIntegrityError)
    assert "attempt 1" in str(excinfo.value.__cause__)
    assert paths.active_pointer.read_text() == original_pointer

    monkeypatch.undo()
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    real_atomic = _atomic_json

    def fail_pointer(path: Path, payload: dict[str, Any]) -> None:
        if path == paths.active_pointer:
            raise OSError("promotion interrupted")
        real_atomic(path, payload)

    monkeypatch.setattr("rebrickable.refresh._atomic_json", fail_pointer)
    with pytest.raises(OSError, match="promotion"):
        await refresh_catalog(catalog_config)
    assert paths.active_pointer.read_text() == original_pointer
    assert {child.name for child in paths.snapshots_dir.iterdir()} == {
        "fixture-snapshot"
    }


@pytest.mark.parametrize("mode", ["activated", "ambiguous", "cleanup-failure"])
async def test_promotion_failure_cleanup_never_deletes_possible_active_snapshot(
    catalog_config: Config, monkeypatch, mode: str
) -> None:
    from rebrickable import refresh as module  # noqa: PLC0415 - patch target

    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_bytes()
    monkeypatch.setattr(
        module, "_download_one", downloader(snapshot_sources(catalog_config))
    )

    def fail_pointer(path: Path, payload: dict[str, Any]) -> None:
        if path != paths.active_pointer:
            _atomic_json(path, payload)
            return
        if mode == "activated":
            _atomic_json(path, payload)
        raise OSError("pointer durability failed")

    monkeypatch.setattr(module, "_atomic_json", fail_pointer)
    if mode == "ambiguous":
        monkeypatch.setattr(module, "_read_pointer", lambda _paths: "")
    if mode == "cleanup-failure":

        def fail_cleanup(*args, **kwargs):
            raise OSError("cleanup also failed")

        monkeypatch.setattr(module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(OSError, match="pointer durability failed"):
        await refresh_catalog(catalog_config)
    assert len(tuple(paths.snapshots_dir.iterdir())) == 2
    if mode == "activated":
        assert paths.active_pointer.read_bytes() != original_pointer
    else:
        assert paths.active_pointer.read_bytes() == original_pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    with FileLock(paths.lock_file, timeout=0):
        pass


@pytest.mark.asyncio
async def test_promotion_lock_timeout_preserves_active_snapshot(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_text()
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))

    def fail_acquire(lock: object) -> None:
        raise Timeout(str(getattr(lock, "lock_file", paths.lock_file)))

    monkeypatch.setattr("rebrickable.refresh.FileLock.acquire", fail_acquire)
    with pytest.raises(CatalogImportError, match="promotion lock"):
        await refresh_catalog(catalog_config)
    assert paths.active_pointer.read_text() == original_pointer


@pytest.mark.asyncio
async def test_schema_mismatch_refresh_reimports_despite_304s(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    manifest = paths.snapshots_dir / "fixture-snapshot" / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["schema_version"] = 999
    manifest.write_text(json.dumps(payload))
    assert (await catalog_state(catalog_config)).status is CatalogStatus.SCHEMA_MISMATCH

    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(sources, unchanged=True)
    )
    report = await refresh_catalog(catalog_config)
    assert report.outcome is RefreshOutcome.UPDATED
    assert report.snapshot_id != "fixture-snapshot"
    assert (await catalog_state(catalog_config)).status is CatalogStatus.READY
    async with await RebrickableSession.open(catalog_config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"


@pytest.mark.asyncio
async def test_corrupt_database_refresh_reimports_despite_304s(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = snapshot_sources(catalog_config)
    paths.database_for("fixture-snapshot").write_bytes(b"not a sqlite database")
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.UNREADABLE

    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(sources, unchanged=True)
    )
    report = await refresh_catalog(catalog_config)

    assert report.outcome is RefreshOutcome.UPDATED
    assert report.snapshot_id != "fixture-snapshot"
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    async with await RebrickableSession.open(catalog_config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"


@pytest.mark.asyncio
async def test_missing_copy_forward_source_raises_download_error(
    catalog_config: Config, monkeypatch
) -> None:
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(sources, unchanged=True)
    )
    sources["parts"].unlink()
    with pytest.raises(DownloadError, match="unable to reuse cached"):
        await refresh_catalog(catalog_config)


@pytest.mark.asyncio
async def test_promotion_prunes_superseded_snapshots_and_orphans(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    (paths.snapshots_dir / ".incoming-stale").mkdir()
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    report = await refresh_catalog(catalog_config)
    remaining = {child.name for child in paths.snapshots_dir.iterdir()}
    assert remaining == {report.snapshot_id}
    pointer = json.loads(paths.active_pointer.read_text())
    assert pointer["snapshot_id"] == report.snapshot_id
    snapshot = paths.snapshots_dir / report.snapshot_id
    assert len(tuple(snapshot.glob("*.csv.gz"))) == 12
    assert (snapshot / "manifest.json").is_file()
    assert (snapshot / catalog_config.database_path.name).is_file()


@pytest.mark.asyncio
async def test_snapshot_retention_keeps_prior_snapshots(
    catalog_config: Config, monkeypatch
) -> None:
    config = replace(catalog_config, snapshot_retention=2)
    paths = CatalogPaths.from_config(config)
    sources = snapshot_sources(config)
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    report = await refresh_catalog(config)
    remaining = {child.name for child in paths.snapshots_dir.iterdir()}
    assert remaining == {"fixture-snapshot", report.snapshot_id}


@pytest.mark.asyncio
async def test_promotion_uses_current_snapshot_and_protects_it_from_orphans(
    catalog_config: Config, monkeypatch
) -> None:
    config = replace(catalog_config, snapshot_retention=2)
    paths = CatalogPaths.from_config(config)
    previous_id = "previous-activated"
    shutil.copytree(
        paths.snapshots_dir / "fixture-snapshot", paths.snapshots_dir / previous_id
    )
    with closing(sqlite3.connect(paths.database_for(previous_id))) as connection:
        connection.execute(
            "INSERT INTO api_crosswalk_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("part", "ldraw", "newly-cached", "3001", "op", "2026-08-01", "abc"),
        )
        connection.commit()
    (paths.snapshots_dir / "zzzz-unactivated-orphan").mkdir()
    acquire = FileLock.acquire

    def promote_before_acquire(lock, *args, **kwargs):
        result = acquire(lock, *args, **kwargs)
        paths.active_pointer.write_text(json.dumps({"snapshot_id": previous_id}))
        return result

    monkeypatch.setattr(FileLock, "acquire", promote_before_acquire)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(snapshot_sources(config))
    )
    report = await refresh_catalog(config)
    monkeypatch.undo()
    assert {child.name for child in paths.snapshots_dir.iterdir()} == {
        previous_id,
        report.snapshot_id,
        "fixture-snapshot",
        "zzzz-unactivated-orphan",
    }
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT canonical_id FROM api_crosswalk_cache WHERE external_id='newly-cached'"
        ).fetchone() == ("3001",)
    assert (await catalog_state(config, verify=True)).snapshot_id == report.snapshot_id


@pytest.mark.asyncio
async def test_carry_crosswalk_drops_vanished_canonicals(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    active_db = (
        paths.snapshots_dir / "fixture-snapshot" / catalog_config.database_path.name
    )
    connection = sqlite3.connect(active_db)
    connection.executemany(
        "INSERT INTO api_crosswalk_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ("part", "ldraw", "kept", "3001", "op", "2026-08-01", "abc"),
            ("part", "ldraw", "orphan", "gone", "op", "2026-08-01", "abc"),
            ("color", "ldraw", "7", "12345", "op", "2026-08-01", "abc"),
        ),
    )
    connection.commit()
    connection.close()
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr("rebrickable.refresh._download_one", downloader(sources))
    report = await refresh_catalog(catalog_config)
    connection = sqlite3.connect(paths.database_for(report.snapshot_id))
    rows = connection.execute(
        "SELECT entity_kind, external_id, canonical_id FROM api_crosswalk_cache"
    ).fetchall()
    connection.close()
    assert ("part", "kept", "3001") in rows
    assert all(row[2] not in {"gone", "12345"} for row in rows)


@pytest.mark.asyncio
async def test_unchanged_refresh_does_not_prune(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    (paths.snapshots_dir / "older-snapshot").mkdir()
    sources = snapshot_sources(catalog_config)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one", downloader(sources, unchanged=True)
    )
    report = await refresh_catalog(catalog_config)
    assert report.outcome is RefreshOutcome.UNCHANGED
    remaining = {child.name for child in paths.snapshots_dir.iterdir()}
    assert remaining == {"fixture-snapshot", "older-snapshot"}


@pytest.mark.asyncio
async def test_refresh_cancellation_preserves_active_snapshot(
    catalog_config: Config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_text()

    started = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        started.set()
        # A suspended await deliberately simulates an interrupted download.
        await asyncio.Future()

    monkeypatch.setattr("rebrickable.refresh._download_one", wait_forever)
    task = asyncio.create_task(refresh_catalog(catalog_config))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paths.active_pointer.read_text() == original_pointer
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


class StreamResponse:
    def __init__(self, status: int, chunks: tuple[bytes, ...] = ()) -> None:
        self.status_code = status
        self.headers = {"etag": '"one"'}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise OSError("bad response")

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk


class StreamClient:
    def __init__(self, response: StreamResponse | BaseException) -> None:
        self.response = response
        self.headers: dict[str, str] | None = None

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        del method, url
        self.headers = headers
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_streaming_downloader_changed_unchanged_and_error(
    tmp_path: Path, catalog_config: Config
) -> None:
    previous = (await catalog_state(catalog_config)).files[0]
    events: list[ProgressEvent] = []
    changed, metadata = await _download_one(
        StreamClient(StreamResponse(200, (b"ab", b"cd"))),
        url="https://example.test/file.csv.gz",
        destination=tmp_path / "file.csv.gz",
        previous=previous,
        force=False,
        callback=events.append,
    )
    assert changed and metadata["byte_length"] == 4
    assert events[-1].current == 4
    not_changed, metadata = await _download_one(
        StreamClient(StreamResponse(304)),
        url="https://example.test/file.csv.gz",
        destination=tmp_path / "unused.csv.gz",
        previous=previous,
        force=False,
        callback=None,
    )
    assert not not_changed and metadata["dataset"] == previous.dataset
    with pytest.raises(DownloadError, match="without a prior fingerprint"):
        await _download_one(
            StreamClient(StreamResponse(304)),
            url="https://example.test/file.csv.gz",
            destination=tmp_path / "missing.csv.gz",
            previous=None,
            force=False,
            callback=None,
        )
    with pytest.raises(DownloadError):
        await _download_one(
            StreamClient(OSError("offline")),
            url="https://example.test/file.csv.gz",
            destination=tmp_path / "bad.csv.gz",
            previous=None,
            force=True,
            callback=None,
        )


@pytest.mark.asyncio
async def test_catalog_state_classifications(catalog_config: Config) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = json.loads(paths.active_pointer.read_text())
    snapshot = paths.snapshots_dir / pointer["snapshot_id"]
    manifest = snapshot / "manifest.json"
    database = snapshot / catalog_config.database_path.name

    original_manifest = manifest.read_text()
    manifest.unlink()
    assert (await catalog_state(catalog_config)).status is CatalogStatus.IMPORT_REQUIRED
    database.unlink()
    assert (await catalog_state(catalog_config)).status is CatalogStatus.UNREADABLE

    database.write_text("not sqlite")
    manifest.write_text("bad json")
    assert (await catalog_state(catalog_config)).status is CatalogStatus.UNREADABLE
    payload = json.loads(original_manifest)
    payload["schema_version"] = 999
    manifest.write_text(json.dumps(payload))
    assert (await catalog_state(catalog_config)).status is CatalogStatus.SCHEMA_MISMATCH

    payload["schema_version"] = 1
    manifest.write_text(json.dumps(payload))
    assert (await catalog_state(catalog_config)).status is CatalogStatus.READY
    verified = await catalog_state(catalog_config, verify=True)
    assert verified.status is CatalogStatus.UNREADABLE


def test_atomic_json_replaces_and_cleans_failure(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "value.json"
    _atomic_json(target, {"b": 2, "a": 1})
    assert target.read_text() == '{"a":1,"b":2}\n'

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("no")

    monkeypatch.setattr("rebrickable._atomic._replace", fail_replace)
    with pytest.raises(OSError):
        _atomic_json(target, {"a": 2})
    assert not tuple(tmp_path.glob(".value.json.*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_downloader_revalidation_headers_and_forced_download(
    catalog_config: Config, tmp_path: Path, *, force: bool
) -> None:
    previous = replace(
        (await catalog_state(catalog_config)).files[0],
        etag='"cached"',
        last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
    )
    client = StreamClient(StreamResponse(200 if force else 304, (b"new data",)))
    destination = tmp_path / "dataset.csv.gz"
    changed, metadata = await _download_one(
        client,
        url=previous.url,
        destination=destination,
        previous=previous,
        force=force,
        callback=None,
    )

    assert changed is force
    if force:
        assert client.headers == {"Accept-Encoding": "identity"}
        assert destination.read_bytes() == b"new data"
        assert metadata["byte_length"] == 8
    else:
        assert client.headers == {
            "Accept-Encoding": "identity",
            "If-None-Match": previous.etag,
            "If-Modified-Since": previous.last_modified,
        }
        assert metadata == asdict(previous)
        assert not destination.exists()


def test_staging_cleanup_preserves_recent_and_unrelated_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000.0
    original_time = time.time
    monkeypatch.setattr(refresh_module, "time", SimpleNamespace(time=lambda: now))
    assert time.time is original_time
    expired = tmp_path / "refresh-expired"
    recent = tmp_path / "refresh-recent"
    unrelated = tmp_path / "other-cache"
    for directory in (expired, recent, unrelated):
        directory.mkdir()
        (directory / "contents").write_text("preserve unless expired staging")
    for directory in (expired, unrelated):
        os.utime(directory, (now - 86_401, now - 86_401))
    os.utime(recent, (now - 86_400, now - 86_400))
    regular_file = tmp_path / "refresh-file"
    regular_file.write_text("keep")

    _prune_abandoned_staging(tmp_path)

    assert not expired.exists()
    assert (recent / "contents").is_file()
    assert (unrelated / "contents").is_file()
    assert regular_file.read_text() == "keep"
    assert time.time is original_time


def test_staging_cleanup_tolerates_removal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "refresh-locked"
    staging.mkdir()
    os.utime(staging, (0, 0))
    attempted: list[Path] = []

    def deny_removal(path: Path) -> None:
        attempted.append(path)
        raise PermissionError("staging directory is locked")

    monkeypatch.setattr("rebrickable.refresh.shutil.rmtree", deny_removal)
    _prune_abandoned_staging(tmp_path)
    assert attempted == [staging]
    assert staging.is_dir()


def test_snapshot_retention_does_not_count_partial_transfers(
    catalog_config: Config,
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    older = paths.snapshots_dir / "older-snapshot"
    oldest = paths.snapshots_dir / "oldest-snapshot"
    incoming = paths.snapshots_dir / ".incoming-stale"
    older.mkdir()
    oldest.mkdir()
    incoming.mkdir()

    _prune_snapshots(
        paths,
        "fixture-snapshot",
        retain=2,
        history=("fixture-snapshot", older.name, oldest.name),
    )

    assert paths.database_for("fixture-snapshot").is_file()
    assert older.is_dir()
    assert not oldest.exists()
    assert not incoming.exists()
