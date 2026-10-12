from __future__ import annotations

import asyncio
import gzip
import sys
import zlib
from collections.abc import AsyncIterator

import httpx2
import pytest


class ByteStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        error: Exception | None = None,
        started: asyncio.Event | None = None,
        resume: asyncio.Event | None = None,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.started = started
        self.resume = resume
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.started is not None:
            self.started.set()
        if self.resume is not None:
            await self.resume.wait()
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


def encode_body(body: bytes, encoding: str) -> bytes:
    match encoding:
        case "gzip":
            return gzip.compress(body, mtime=0)
        case "deflate":
            return zlib.compress(body)
        case "gzip, deflate":
            return zlib.compress(gzip.compress(body, mtime=0))
        case "zstd":
            if sys.version_info < (3, 14):
                pytest.skip("Zstandard uses the Python 3.14 standard library")
            from compression import zstd  # noqa: PLC0415 - optional Python 3.14 module

            return zstd.compress(body)
        case _:
            raise ValueError(f"unsupported test encoding: {encoding}")
