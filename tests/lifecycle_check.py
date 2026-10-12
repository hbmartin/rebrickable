"""Bounded subprocess checks for resource lifecycles that can hang teardown."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
from filelock import FileLock, Timeout

from rebrickable import Config, RebrickableSession
from rebrickable.catalog.database import CatalogPaths, open_catalog
from tests.catalog_fixture import build_catalog_config


async def check_close(config: Config, *, cancel: bool) -> None:
    session = await RebrickableSession.connect(config)
    connection = await session._connection()
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0
    original_close = aiosqlite.Connection.close

    async def close(current: aiosqlite.Connection) -> None:
        nonlocal calls
        if current is connection:
            calls += 1
            started.set()
            await finish.wait()
        await original_close(current)

    with patch.object(aiosqlite.Connection, "close", close):
        first = asyncio.create_task(session.close())
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(session.close())
        reopening = (
            asyncio.create_task(session.parts.require("3001")) if cancel else None
        )
        try:
            for _ in range(3):
                if cancel:
                    first.cancel()
                await asyncio.sleep(0)
                assert not first.done()
                assert not second.done()
                assert session._db is None
                assert session._opened_state is None
                if reopening is not None:
                    assert not reopening.done()
        finally:
            finish.set()
        if cancel:
            try:
                await first
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("close did not propagate cancellation")
        else:
            await first
        await asyncio.wait_for(second, 5)
        assert calls == 1
        assert connection._connection is None
        assert not connection._thread.is_alive()
        if reopening is not None:
            assert (await asyncio.wait_for(reopening, 5)).part_num == "3001"
            assert session._db is not connection
            assert (
                await session.opened_catalog_state()
            ).snapshot_id == "fixture-snapshot"
        await session.close()


async def check_verification(config: Config) -> None:
    factory = aiosqlite.connect
    opened: list[aiosqlite.Connection] = []
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def connect(*args: Any, **kwargs: Any) -> aiosqlite.Connection:
        connection = factory(*args, **kwargs)
        opened.append(connection)
        connector = connection._connector

        def delayed() -> Any:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("verification worker was not released")
            return connector()

        connection._connector = delayed
        return connection

    with patch.object(aiosqlite, "connect", connect):
        task = asyncio.create_task(open_catalog(config))
        try:
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            try:
                with FileLock(CatalogPaths.from_config(config).lock_file, timeout=0):
                    raise AssertionError("verification released its lock too early")
            except Timeout:
                pass
        finally:
            release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("verification did not propagate cancellation")
    assert len(opened) == 1
    assert opened[0]._connection is None
    assert not opened[0]._thread.is_alive()
    with FileLock(CatalogPaths.from_config(config).lock_file, timeout=0):
        pass
    async with await RebrickableSession.connect(config) as session:
        assert await session.parts.require("3001")


if __name__ == "__main__":
    config = build_catalog_config(Path(sys.argv[2]))
    asyncio.run(
        check_verification(config)
        if sys.argv[1] == "verification"
        else check_close(config, cancel=sys.argv[1] == "cancel-close-reopen")
    )
