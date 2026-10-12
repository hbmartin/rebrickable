"""Wait for resource-owning workers before propagating cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from filelock import FileLock

P = ParamSpec("P")
T = TypeVar("T")


async def settle(awaitable: Awaitable[T]) -> T:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # Retrieve failures while preserving caller cancellation.
        raise


async def run_in_thread(
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    return await settle(asyncio.to_thread(function, *args, **kwargs))


async def acquire_lock(lock: FileLock) -> None:
    try:
        await run_in_thread(lock.acquire)
    except asyncio.CancelledError:
        lock.release()
        raise
