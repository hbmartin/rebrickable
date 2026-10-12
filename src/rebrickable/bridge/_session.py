"""Structural session contract used by the optional LDraw bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import aiosqlite

    from rebrickable.config import Config
    from rebrickable.types import CatalogState


class BridgeSession(Protocol):
    @property
    def config(self) -> Config: ...

    @property
    def _opened_state(self) -> CatalogState | None: ...

    async def _connection(self) -> aiosqlite.Connection: ...

    async def state(self, *, verify: bool = False) -> CatalogState: ...
