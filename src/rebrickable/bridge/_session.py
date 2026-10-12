"""Structural session contract used by the optional LDraw bridge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import aiosqlite

    from rebrickable.config import Config
    from rebrickable.types import CatalogState


class BridgeSession(Protocol):
    @property
    def config(self) -> Config:
        """Return the session's configuration for bridge operations."""

    @property
    def _opened_state(self) -> CatalogState | None:
        """Report the catalog snapshot currently pinned to the session."""

    async def _connection(self) -> aiosqlite.Connection:
        """Return the pinned catalog connection."""

    async def state(self, *, verify: bool = False) -> CatalogState:
        """Resolve the active catalog state with optional integrity verification."""
