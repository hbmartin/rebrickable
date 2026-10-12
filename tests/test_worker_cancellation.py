from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock, Timeout

from rebrickable import Config, RebrickableSession
from rebrickable._workers import settle
from rebrickable.api.models import ApiColor, ApiPart
from rebrickable.bridge import cache
from rebrickable.bridge.overrides import read_overrides
from rebrickable.catalog.database import CatalogPaths, catalog_state, open_catalog
from rebrickable.catalog.importers import import_catalog
from rebrickable.refresh import refresh_catalog
from tests.test_refresh_state import downloader, snapshot_sources


@pytest.mark.parametrize("operation", ["open", "refresh"])
async def test_cancelled_lock_acquisition_releases_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    acquire = FileLock.acquire

    def delayed(lock: FileLock, *args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release worker")
        return acquire(lock, *args, **kwargs)

    monkeypatch.setattr(FileLock, "acquire", delayed)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one",
        downloader(snapshot_sources(catalog_config)),
    )
    task = asyncio.create_task(
        open_catalog(catalog_config)
        if operation == "open"
        else refresh_catalog(catalog_config)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    with FileLock(paths.lock_file, timeout=0):
        pass
    assert paths.active_pointer.read_bytes() == pointer
    async with await RebrickableSession.connect(catalog_config) as session:
        assert await session.parts.require("3001")


async def test_cancelled_import_finishes_before_staging_cleanup(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    completed: list[Path] = []

    def delayed(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release worker")
        result = import_catalog(*args, **kwargs)
        completed.append(args[1])
        return result

    monkeypatch.setattr("rebrickable.refresh.import_catalog", delayed)
    monkeypatch.setattr(
        "rebrickable.refresh._download_one",
        downloader(snapshot_sources(catalog_config)),
    )
    task = asyncio.create_task(refresh_catalog(catalog_config))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated cancellation must still drain the worker.
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed
    assert not completed[0].exists()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert paths.active_pointer.read_bytes() == pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).snapshot_id == "fixture-snapshot"


@pytest.mark.parametrize(
    "failure",
    ["inspect_header", "materialize_overrides", "_carry_crosswalk", "durable_replace"],
)
async def test_pre_promotion_fault_preserves_readable_snapshot(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(
        "rebrickable.refresh._download_one",
        downloader(snapshot_sources(catalog_config)),
    )
    monkeypatch.setattr(f"rebrickable.refresh.{failure}", fail)
    with pytest.raises(OSError, match="injected failure"):
        await refresh_catalog(catalog_config)
    assert paths.active_pointer.read_bytes() == pointer
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
    with FileLock(paths.lock_file, timeout=0):
        pass
    assert (
        await catalog_state(catalog_config, verify=True)
    ).snapshot_id == "fixture-snapshot"


async def test_open_connection_failure_closes_and_releases(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite  # noqa: PLC0415 - local patch target

    execute = aiosqlite.Connection.execute
    closed: list[bool] = []
    close = aiosqlite.Connection.close

    async def fail(
        connection: aiosqlite.Connection, sql: str, parameters: Any = None
    ) -> Any:
        if sql == "PRAGMA query_only = ON":
            raise OSError("setup failed")
        return await execute(connection, sql, parameters)

    async def record_close(connection: aiosqlite.Connection) -> None:
        await close(connection)
        closed.append(True)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail)
    monkeypatch.setattr(aiosqlite.Connection, "close", record_close)
    with pytest.raises(OSError, match="setup failed"):
        await open_catalog(catalog_config)
    assert len(closed) == 2
    with FileLock(CatalogPaths.from_config(catalog_config).lock_file, timeout=0):
        pass


async def test_worker_cancellation_retrieves_worker_failure() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fail() -> None:
        started.set()
        await finish.wait()
        raise OSError("worker failed")

    task = asyncio.create_task(settle(fail()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_self_cancellation() -> None:
    async def cancel() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await settle(cancel())


@pytest.mark.parametrize(
    "phase",
    [
        "_carry_crosswalk",
        "activation_marker",
        "move",
        "durable_replace",
        "_atomic_json",
        "_prune_snapshots",
    ],
)
@pytest.mark.parametrize("retain", [1, 2])
async def test_cancelled_promotion_leaves_complete_readable_snapshot(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, phase: str, retain: int
) -> None:
    from rebrickable import refresh as module  # noqa: PLC0415 - patch target

    catalog_config = replace(catalog_config, snapshot_retention=retain)
    paths = CatalogPaths.from_config(catalog_config)
    (paths.snapshots_dir / "zzzz-unactivated-orphan").mkdir()
    before = paths.active_pointer.read_bytes()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    target = module.shutil if phase == "move" else module
    attribute = "_atomic_json" if phase == "activation_marker" else phase
    original: Callable[..., Any] = getattr(target, attribute)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        if phase == "durable_replace" and not args[0].is_dir():
            return original(*args, **kwargs)
        if phase == "_atomic_json" and args[0] != paths.active_pointer:
            return original(*args, **kwargs)
        if phase == "activation_marker" and args[0].name != module._ACTIVATION_MARKER:
            return original(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("test did not release worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attribute, delayed)
    monkeypatch.setattr(
        module, "_download_one", downloader(snapshot_sources(catalog_config))
    )
    task = asyncio.create_task(refresh_catalog(catalog_config))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paths.active_pointer.read_bytes() != before
    state = await catalog_state(catalog_config, verify=True)
    expected = {state.snapshot_id, "zzzz-unactivated-orphan"}
    if retain == 2:
        expected.add("fixture-snapshot")
        assert paths.database_for("fixture-snapshot").is_file()
    assert {child.name for child in paths.snapshots_dir.iterdir()} == expected
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
    with FileLock(paths.lock_file, timeout=0):
        pass
    async with await RebrickableSession.connect(catalog_config) as session:
        assert await session.parts.require("3001")


async def test_cancelled_connection_open_closes_worker_connection(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite  # noqa: PLC0415 - patch target

    factory = aiosqlite.connect
    opened: list[aiosqlite.Connection] = []
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        connection = factory(*args, **kwargs)
        opened.append(connection)
        if len(opened) == 2:
            connector = connection._connector

            def delayed() -> Any:
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("test did not release worker")
                return connector()

            connection._connector = delayed
        return connection

    monkeypatch.setattr("rebrickable.catalog.database.aiosqlite.connect", connect)
    task = asyncio.create_task(open_catalog(catalog_config))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(opened) == 2
    assert opened[1]._connection is None
    with FileLock(CatalogPaths.from_config(catalog_config).lock_file, timeout=0):
        pass


async def test_cancelled_session_close_drains_and_can_reopen(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite  # noqa: PLC0415 - patch target

    session = await RebrickableSession.connect(catalog_config)
    connection = await session._connection()
    started = asyncio.Event()
    finish = asyncio.Event()
    close = aiosqlite.Connection.close

    async def delayed(connection: aiosqlite.Connection) -> None:
        started.set()
        await finish.wait()
        await close(connection)

    monkeypatch.setattr(aiosqlite.Connection, "close", delayed)
    task = asyncio.create_task(session.close())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection._connection is None
    assert session._db is None
    assert (await session.parts.require("3001")).part_num == "3001"
    await session.close()


@pytest.mark.parametrize(
    "scenario", ["double-close", "cancel-close-reopen", "verification"]
)
def test_lifecycle_in_bounded_subprocess(tmp_path: Path, scenario: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.lifecycle_check",
            scenario,
            str(tmp_path / "catalog"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stderr == ""


@pytest.mark.parametrize(
    "operation", ["part-cache", "color-cache", "add-override", "remove-override"]
)
async def test_cancelled_bridge_write_finishes_and_releases_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import sqlite3  # noqa: PLC0415 - local database inspection

    paths = CatalogPaths.from_config(catalog_config)
    session = await RebrickableSession.open(catalog_config)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    update = cache.update_search_document

    def delayed(*args: Any, **kwargs: Any) -> None:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("bridge worker was not released")
        update(*args, **kwargs)

    class Client:
        async def get_part(self, *_args: object, **_kwargs: object) -> ApiPart:
            return ApiPart(
                part_num="3001", name="Brick", external_ids={"LDraw": ["threaded"]}
            )

        async def get_color(self, *_args: object) -> ApiColor:
            return ApiColor(
                id=4, name="Red", rgb="C91A09", external_ids={"LDraw": [14]}
            )

    monkeypatch.setattr(cache, "update_search_document", delayed)
    if operation == "part-cache":
        pending = session.ldraw.enrich_part_mapping("3001", Client())
    elif operation == "color-cache":
        pending = session.ldraw.enrich_color_mapping(4, Client())
    elif operation == "add-override":
        pending = session.ldraw.record_part_override("threaded.dat", "3001")
    else:
        pending = session.ldraw.remove_part_override("3001")
    task = asyncio.create_task(pending)
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        with pytest.raises(Timeout), FileLock(paths.lock_file, timeout=0):
            pass
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with FileLock(paths.lock_file, timeout=0):
        pass
    with closing(sqlite3.connect(paths.database_for("fixture-snapshot"))) as connection:
        if operation.endswith("cache"):
            external = "threaded" if operation == "part-cache" else "14"
            row = connection.execute(
                "SELECT external_id FROM api_crosswalk_cache WHERE external_id=?",
                (external,),
            ).fetchone()
            assert row == (external,)
        else:
            source = "threaded" if operation == "add-override" else "3001"
            rows = connection.execute(
                "SELECT source_id FROM user_mapping_overrides WHERE source_id=?",
                (source,),
            ).fetchall()
            assert bool(rows) is (operation == "add-override")
            assert catalog_config.mapping_overrides_path is not None
            records = read_overrides(catalog_config.mapping_overrides_path)
            assert any(item["source_id"] == source for item in records) is bool(rows)
    await session.close()
