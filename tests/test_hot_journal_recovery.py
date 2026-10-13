from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from filelock import FileLock, Timeout

from rebrickable import CatalogStatus
from rebrickable import refresh as module
from rebrickable.bridge.cache import store_crosswalks
from rebrickable.catalog.database import CatalogPaths, _CatalogInspection, catalog_state
from rebrickable.errors import CatalogImportError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


def crash_writer(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE journal_padding(value BLOB)")
        connection.executemany(
            "INSERT INTO journal_padding VALUES (?)", [(b"a" * 4000,)] * 300
        )
        connection.commit()
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA cache_size=2')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE api_crosswalk_cache SET external_id='uncommitted'")
connection.execute('UPDATE journal_padding SET value=randomblob(4000)')
os._exit(0)
""",
            str(database),
        ],
        check=True,
        timeout=10,
    )
    assert Path(f"{database}-journal").stat().st_size > 512


def seed_mapping(config) -> None:
    store_crosswalks(
        config,
        entity_kind="part",
        external_system="ldraw",
        canonical_id="3001",
        external_ids=("journal-mapping",),
        operation_id="op",
        response_payload={},
    )


@pytest.mark.parametrize("when", ["initial", "during"])
async def test_refresh_recovers_hot_journal_without_losing_mappings(
    catalog_config, monkeypatch, when
) -> None:
    config = replace(catalog_config, snapshot_retention=1)
    paths = CatalogPaths.from_config(config)
    seed_mapping(config)
    sources = stable_sources(config)
    previous = paths.database_for("fixture-snapshot")
    if when == "initial":
        crash_writer(previous)
    original = module._inspect_catalog

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> _CatalogInspection:
        if when == "during" and not verify:
            crash_writer(previous)
        return await original(config, pointer, verify=verify, verification=verification)

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    factory = sqlite3.connect
    recoveries = []

    def connect(database, *args: Any, **kwargs: Any) -> Any:
        if str(database).endswith("?mode=rw"):
            with pytest.raises(Timeout), FileLock(paths.lock_file, timeout=0):
                pass
            recoveries.append(database)
        return factory(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    events = []
    report = await module.refresh_catalog(config, callback=events.append)
    assert len(recoveries) == 1
    assert not previous.exists()
    assert not any("skipped cached" in event.message for event in events)
    with closing(factory(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT external_id FROM api_crosswalk_cache"
        ).fetchall() == [("journal-mapping",)]
        assert connection.execute(
            "SELECT source_id FROM user_mapping_overrides WHERE entity_kind='part'"
        ).fetchone() == ("3001",)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH 'journal'"
            ).fetchone()[0]
            > 0
        )
    assert (await catalog_state(config, verify=True)).snapshot_id == report.snapshot_id
    assert not tuple(config.cache_path.glob("refresh-*"))


async def test_ordinary_verification_does_not_repair_hot_journal(
    catalog_config,
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = paths.database_for("fixture-snapshot")
    crash_writer(previous)
    journal = Path(f"{previous}-journal")
    before_database = previous.read_bytes()
    before_journal = await asyncio.to_thread(journal.read_bytes)
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.UNREADABLE
    assert previous.read_bytes() == before_database
    assert await asyncio.to_thread(journal.read_bytes) == before_journal


async def test_hot_journal_recovery_failure_keeps_pointer_and_cache(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    seed_mapping(catalog_config)
    sources = stable_sources(catalog_config)
    crash_writer(paths.database_for("fixture-snapshot"))
    before = paths.active_pointer.read_bytes()
    original = sqlite3.connect
    error = sqlite3.OperationalError("source is busy during recovery")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    attempts = Mock()

    def connect(database, *args: Any, **kwargs: Any) -> Any:
        if str(database).endswith("?mode=rw"):
            attempts()
            raise error
        return original(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    with pytest.raises(CatalogImportError, match="source is busy"):
        await module.refresh_catalog(catalog_config)
    attempts.assert_called_once()
    assert paths.active_pointer.read_bytes() == before
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    with closing(original(paths.database_for("fixture-snapshot"))) as connection:
        assert connection.execute(
            "SELECT external_id FROM api_crosswalk_cache"
        ).fetchone() == ("journal-mapping",)
    with FileLock(paths.lock_file, timeout=0):
        pass


async def test_cancellation_drains_journal_recovery_and_activation(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    crash_writer(paths.database_for("fixture-snapshot"))
    original = sqlite3.connect
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    opened = []

    class Recovery:
        def __init__(self, connection) -> None:
            self.connection = connection
            self.closed = False

        def execute(self, sql) -> sqlite3.Cursor:
            if sql == "PRAGMA quick_check":
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("recovery was not released")
            return self.connection.execute(sql)

        def close(self) -> None:
            self.connection.close()
            self.closed = True

    def connect(database, *args: Any, **kwargs: Any) -> Any:
        connection = original(database, *args, **kwargs)
        if str(database).endswith("?mode=rw"):
            connection = Recovery(connection)
            opened.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    task = asyncio.create_task(module.refresh_catalog(catalog_config))
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
    assert len(opened) == 1
    assert opened[0].closed
    state = await catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.READY
    assert state.snapshot_id != "fixture-snapshot"
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
    with FileLock(paths.lock_file, timeout=0):
        pass
