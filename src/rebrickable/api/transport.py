"""Injectable asynchronous HTTP transport boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx2


class ResponseLike(Protocol):
    @property
    # Protocol/overload declarations intentionally have no implementation.
    # codeql[py/ineffectual-statement]  # noqa: ERA001 - CodeQL suppression directive
    def status_code(self) -> int: ...

    @property
    # Protocol/overload declarations intentionally have no implementation.
    # codeql[py/ineffectual-statement]  # noqa: ERA001 - CodeQL suppression directive
    def headers(self) -> Mapping[str, str]: ...

    @property
    # Protocol/overload declarations intentionally have no implementation.
    # codeql[py/ineffectual-statement]  # noqa: ERA001 - CodeQL suppression directive
    def text(self) -> str: ...

    # Protocol/overload declarations intentionally have no implementation.
    # codeql[py/ineffectual-statement]  # noqa: ERA001 - CodeQL suppression directive
    def json(self) -> Any: ...


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
        # Protocol declaration intentionally has no implementation.
        # codeql[py/ineffectual-statement]  # noqa: ERA001 - CodeQL suppression directive
    ) -> ResponseLike: ...


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
