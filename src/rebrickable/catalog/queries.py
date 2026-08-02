"""Session-bound repository facades."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, TypeVar

import aiosqlite

from rebrickable.catalog.inventory import bill_of_materials, load_inventory
from rebrickable.catalog.models import (
    BomRow,
    Color,
    Element,
    Inventory,
    Minifig,
    Part,
    PartCategory,
    Set,
    Theme,
)
from rebrickable.errors import EntityNotFoundError

if TYPE_CHECKING:
    from rebrickable.session import RebrickableSession

T = TypeVar("T")


class Repository(Generic[T]):
    def __init__(
        self,
        session: RebrickableSession,
        *,
        kind: str,
        table: str,
        key: str,
        order: str,
        factory: Callable[[aiosqlite.Row], T],
    ) -> None:
        self._session = session
        self._kind = kind
        self._table = table
        self._key = key
        self._order = order
        self._factory = factory

    async def get(self, identifier: str | int) -> T | None:
        connection = await self._session._connection()
        row = await (
            await connection.execute(
                f"SELECT * FROM {self._table} WHERE {self._key}=?",
                (identifier,),
            )
        ).fetchone()
        return None if row is None else self._factory(row)

    async def require(self, identifier: str | int) -> T:
        entity = await self.get(identifier)
        if entity is None:
            raise EntityNotFoundError(self._kind, identifier)
        return entity

    async def list(self, *, limit: int = 50, offset: int = 0) -> tuple[T, ...]:
        if not 1 <= limit <= 1_000 or offset < 0:
            raise ValueError("invalid limit or offset")
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                f"SELECT * FROM {self._table} ORDER BY {self._order} LIMIT ? OFFSET ?",
                (limit, offset),
            )
        ).fetchall()
        return tuple(self._factory(row) for row in rows)


def _part(row: aiosqlite.Row) -> Part:
    return Part(row["part_num"], row["name"], row["part_cat_id"], row["material"])


def _set(row: aiosqlite.Row) -> Set:
    return Set(
        row["set_num"],
        row["name"],
        row["year"],
        row["theme_id"],
        row["num_parts"],
        row["image_url"],
    )


def _minifig(row: aiosqlite.Row) -> Minifig:
    return Minifig(row["fig_num"], row["name"], row["num_parts"], row["image_url"])


def _color(row: aiosqlite.Row) -> Color:
    return Color(
        row["id"],
        row["name"],
        row["rgb"],
        bool(row["is_trans"]),
        row["num_parts"],
        row["num_sets"],
        row["year_from"],
        row["year_to"],
    )


class PartsRepository(Repository[Part]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="part",
            table="parts",
            key="part_num",
            order="part_num",
            factory=_part,
        )


class SetsRepository(Repository[Set]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="set",
            table="sets",
            key="set_num",
            order="set_num",
            factory=_set,
        )

    async def inventory(self, set_num: str) -> Inventory:
        return await load_inventory(await self._session._connection(), set_num)

    async def bill_of_materials(
        self, set_num: str, *, include_spares: bool = False
    ) -> tuple[BomRow, ...]:
        return await bill_of_materials(
            await self._session._connection(), set_num, include_spares=include_spares
        )


class MinifigsRepository(Repository[Minifig]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="minifig",
            table="minifigs",
            key="fig_num",
            order="fig_num",
            factory=_minifig,
        )

    async def inventory(self, fig_num: str) -> Inventory:
        return await load_inventory(await self._session._connection(), fig_num)

    async def bill_of_materials(
        self, fig_num: str, *, include_spares: bool = False
    ) -> tuple[BomRow, ...]:
        return await bill_of_materials(
            await self._session._connection(), fig_num, include_spares=include_spares
        )


class ThemesRepository(Repository[Theme]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="theme",
            table="themes",
            key="id",
            order="id",
            factory=lambda row: Theme(row["id"], row["name"], row["parent_id"]),
        )

    async def children(self, theme_id: int) -> tuple[Theme, ...]:
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                "SELECT * FROM themes WHERE parent_id=? ORDER BY id", (theme_id,)
            )
        ).fetchall()
        return tuple(Theme(row["id"], row["name"], row["parent_id"]) for row in rows)

    async def parent(self, theme_id: int) -> Theme | None:
        theme = await self.require(theme_id)
        return None if theme.parent_id is None else await self.get(theme.parent_id)


class ColorsRepository(Repository[Color]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session, kind="color", table="colors", key="id", order="id", factory=_color
        )


class CategoriesRepository(Repository[PartCategory]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="part category",
            table="part_categories",
            key="id",
            order="id",
            factory=lambda row: PartCategory(row["id"], row["name"]),
        )


class ElementsRepository(Repository[Element]):
    def __init__(self, session: RebrickableSession) -> None:
        super().__init__(
            session,
            kind="element",
            table="elements",
            key="element_id",
            order="element_id",
            factory=lambda row: Element(
                row["element_id"], row["part_num"], row["color_id"], row["design_id"]
            ),
        )
