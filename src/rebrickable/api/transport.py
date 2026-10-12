"""Injectable asynchronous HTTP transport boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx2


class ResponseLike(Protocol):
    @property
    def status_code(self) -> int:
        """Return the HTTP response status code."""

    @property
    def headers(self) -> Mapping[str, str]:
        """Expose the response headers."""

    @property
    def text(self) -> str:
        """Return the decoded response body."""

    def json(self) -> Any:
        """Parse the response body as JSON."""


class AsyncTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> ResponseLike:
        """Send an asynchronous HTTP request and return its response."""


class HttpxTransport:
    def __init__(self, *, timeout: httpx2.Timeout) -> None:
        self._client = httpx2.AsyncClient(timeout=timeout, follow_redirects=False)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> ResponseLike:
        return await self._client.request(
            method, url, headers=headers, params=params, data=data, json=json
        )

    async def close(self) -> None:
        await self._client.aclose()
