"""Session-bound repository facades."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Generic, Protocol, TypeVar

import aiosqlite

from rebrickable.catalog.inventory import bill_of_materials, load_inventory
from rebrickable.catalog.models import (
    CatalogBom,
    Color,
    Element,
    Inventory,
    InventoryDiff,
    InventoryDiffRow,
    InventoryVersion,
    Minifig,
    Part,
    PartCategory,
    PartRelationship,
    PartUsage,
    Set,
    SetOccurrence,
    Theme,
)
from rebrickable.errors import EntityNotFoundError, InventoryNotFoundError
from rebrickable.types import RelationshipType

T = TypeVar("T")


class _SessionProtocol(Protocol):
    async def _connection(self) -> aiosqlite.Connection: ...


class Repository(Generic[T]):
    def __init__(
        self,
        session: _SessionProtocol,
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

    async def count(self) -> int:
        connection = await self._session._connection()
        row = await (
            await connection.execute(f"SELECT COUNT(*) FROM {self._table}")
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def iter(self, *, page_size: int = 500) -> AsyncIterator[T]:
        if not 1 <= page_size <= 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        return self._iter_pages(page_size)

    async def _iter_pages(self, page_size: int) -> AsyncIterator[T]:
        offset = 0
        while page := await self.list(limit=page_size, offset=offset):
            for entity in page:
                yield entity
            offset += len(page)


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
    def __init__(self, session: _SessionProtocol) -> None:
        super().__init__(
            session,
            kind="part",
            table="parts",
            key="part_num",
            order="part_num",
            factory=_part,
        )

    async def relationships(
        self,
        part_num: str,
        *,
        direction: str = "both",
        types: set[RelationshipType] | None = None,
    ) -> tuple[PartRelationship, ...]:
        if direction not in {"both", "parents", "children"}:
            raise ValueError("direction must be both, parents, or children")
        clauses: list[str] = []
        values: list[object] = []
        if direction in {"both", "parents"}:
            clauses.append("child_part_num=?")
            values.append(part_num)
        if direction in {"both", "children"}:
            clauses.append("parent_part_num=?")
            values.append(part_num)
        if types:
            raw_types = sorted(item.value for item in types)
            placeholders = ",".join("?" for _ in raw_types)
            clauses.append(f"rel_type IN ({placeholders})")
            values.extend(raw_types)
        endpoint_clause = (
            f"({' OR '.join(clauses[:2])})" if direction == "both" else clauses[0]
        )
        type_clause = clauses[-1] if types else None
        where = endpoint_clause + (f" AND {type_clause}" if type_clause else "")
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                "SELECT rel_type, child_part_num, parent_part_num "
                f"FROM part_relationships WHERE {where} "
                "ORDER BY rel_type, child_part_num, parent_part_num",
                values,
            )
        ).fetchall()
        return tuple(
            PartRelationship(
                RelationshipType.from_upstream(str(row["rel_type"])),
                str(row["rel_type"]),
                str(row["child_part_num"]),
                str(row["parent_part_num"]),
            )
            for row in rows
        )

    async def canonical_mold(self, part_num: str, *, max_depth: int = 100) -> str:
        """Follow mold-parent edges to their deterministic canonical endpoint."""
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        await self.require(part_num)
        connection = await self._session._connection()
        current = part_num
        seen = {current}
        for _ in range(max_depth):
            row = await (
                await connection.execute(
                    "SELECT parent_part_num FROM part_relationships "
                    "WHERE rel_type=? AND child_part_num=? "
                    "ORDER BY parent_part_num LIMIT 1",
                    (RelationshipType.MOLD.value, current),
                )
            ).fetchone()
            if row is None:
                return current
            parent = str(row[0])
            if parent in seen:
                return min(seen)
            seen.add(parent)
            current = parent
        raise ValueError(f"mold relationship depth exceeds {max_depth}")

    async def variants(self, part_num: str) -> tuple[PartRelationship, ...]:
        return await self.relationships(
            part_num,
            types={
                RelationshipType.MOLD,
                RelationshipType.PRINT,
                RelationshipType.PATTERN,
                RelationshipType.ALTERNATE,
            },
        )

    async def used_in_sets(
        self,
        part_num: str,
        *,
        color_id: int | None = None,
        include_spares: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[SetOccurrence, ...]:
        if not 1 <= limit <= 1_000 or offset < 0:
            raise ValueError("invalid limit or offset")
        await self.require(part_num)
        conditions = ["ip.part_num=?"]
        values: list[object] = [part_num]
        if color_id is not None:
            conditions.append("ip.color_id=?")
            values.append(color_id)
        if not include_spares:
            conditions.append("ip.is_spare=0")
        values.extend((limit, offset))
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                """
                SELECT s.*, c.id AS color_id, c.name AS color_name, c.rgb,
                       c.is_trans, c.num_parts AS color_num_parts,
                       c.num_sets AS color_num_sets, c.year_from, c.year_to,
                       SUM(ip.quantity) AS quantity, ip.is_spare, li.version
                FROM inventory_parts ip
                JOIN latest_inventories li ON li.id=ip.inventory_id
                JOIN sets s ON s.set_num=li.owner_num
                JOIN colors c ON c.id=ip.color_id
                WHERE """
                + " AND ".join(conditions)
                + """
                GROUP BY s.set_num, c.id, ip.is_spare, li.version
                ORDER BY s.year DESC, s.set_num, c.id, ip.is_spare
                LIMIT ? OFFSET ?
                """,
                values,
            )
        ).fetchall()
        return tuple(
            SetOccurrence(
                _set(row),
                Color(
                    row["color_id"],
                    row["color_name"],
                    row["rgb"],
                    bool(row["is_trans"]),
                    row["color_num_parts"],
                    row["color_num_sets"],
                    row["year_from"],
                    row["year_to"],
                ),
                int(row["quantity"]),
                bool(row["is_spare"]),
                int(row["version"]),
            )
            for row in rows
        )

    async def usage_stats(
        self, part_num: str, *, color_id: int | None = None
    ) -> PartUsage:
        part = await self.require(part_num)
        color_clause = "" if color_id is None else " AND ip.color_id=?"
        values: tuple[object, ...] = (
            (part_num,) if color_id is None else (part_num, color_id)
        )
        connection = await self._session._connection()
        row = await (
            await connection.execute(
                """
                SELECT COALESCE(SUM(ip.quantity), 0) AS total_quantity,
                       COALESCE(SUM(CASE WHEN ip.is_spare THEN ip.quantity ELSE 0 END), 0)
                           AS spare_quantity,
                       COUNT(DISTINCT s.set_num) AS set_count,
                       COUNT(DISTINCT li.id) AS inventory_count,
                       COUNT(DISTINCT ip.color_id) AS color_count,
                       MIN(s.year) AS first_year, MAX(s.year) AS last_year
                FROM inventory_parts ip
                JOIN latest_inventories li ON li.id=ip.inventory_id
                LEFT JOIN sets s ON s.set_num=li.owner_num
                WHERE ip.part_num=?"""
                + color_clause,
                values,
            )
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate always yields one row
            raise RuntimeError("part usage aggregate returned no row")
        return PartUsage(
            part,
            int(row["total_quantity"]),
            int(row["spare_quantity"]),
            int(row["set_count"]),
            int(row["inventory_count"]),
            int(row["color_count"]),
            row["first_year"],
            row["last_year"],
        )


class SetsRepository(Repository[Set]):
    def __init__(self, session: _SessionProtocol) -> None:
        super().__init__(
            session,
            kind="set",
            table="sets",
            key="set_num",
            order="set_num",
            factory=_set,
        )

    async def inventory(self, set_num: str, *, version: int | None = None) -> Inventory:
        return await load_inventory(
            await self._session._connection(), set_num, version=version
        )

    async def bill_of_materials(
        self, set_num: str, *, include_spares: bool = False, strict: bool = False
    ) -> CatalogBom:
        return await bill_of_materials(
            await self._session._connection(),
            set_num,
            include_spares=include_spares,
            strict=strict,
        )


class MinifigsRepository(Repository[Minifig]):
    def __init__(self, session: _SessionProtocol) -> None:
        super().__init__(
            session,
            kind="minifig",
            table="minifigs",
            key="fig_num",
            order="fig_num",
            factory=_minifig,
        )

    async def inventory(self, fig_num: str, *, version: int | None = None) -> Inventory:
        return await load_inventory(
            await self._session._connection(), fig_num, version=version
        )

    async def bill_of_materials(
        self, fig_num: str, *, include_spares: bool = False, strict: bool = False
    ) -> CatalogBom:
        return await bill_of_materials(
            await self._session._connection(),
            fig_num,
            include_spares=include_spares,
            strict=strict,
        )


class ThemesRepository(Repository[Theme]):
    def __init__(self, session: _SessionProtocol) -> None:
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

    async def lineage(self, theme_id: int) -> tuple[Theme, ...]:
        await self.require(theme_id)
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                """
                WITH RECURSIVE ancestors(id, name, parent_id, depth) AS (
                    SELECT id, name, parent_id, 0 FROM themes WHERE id=?
                    UNION ALL
                    SELECT t.id, t.name, t.parent_id, a.depth + 1
                    FROM themes t JOIN ancestors a ON t.id=a.parent_id
                    WHERE a.depth < 100
                )
                SELECT id, name, parent_id FROM ancestors ORDER BY depth
                """,
                (theme_id,),
            )
        ).fetchall()
        unique = {row["id"]: row for row in rows}
        return tuple(
            Theme(row["id"], row["name"], row["parent_id"])
            for row in reversed(unique.values())
        )

    async def descendants(self, theme_id: int) -> tuple[Theme, ...]:
        await self.require(theme_id)
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                """
                WITH RECURSIVE descendants(id, name, parent_id, depth) AS (
                    SELECT id, name, parent_id, 0 FROM themes WHERE id=?
                    UNION ALL
                    SELECT t.id, t.name, t.parent_id, d.depth + 1
                    FROM themes t JOIN descendants d ON t.parent_id=d.id
                    WHERE d.depth < 100
                )
                SELECT id, name, parent_id FROM descendants
                WHERE depth > 0 ORDER BY depth, id
                """,
                (theme_id,),
            )
        ).fetchall()
        unique = {row["id"]: row for row in rows}
        return tuple(
            Theme(row["id"], row["name"], row["parent_id"])
            for row in unique.values()
            if row["id"] != theme_id
        )


class ColorsRepository(Repository[Color]):
    def __init__(self, session: _SessionProtocol) -> None:
        super().__init__(
            session, kind="color", table="colors", key="id", order="id", factory=_color
        )


class CategoriesRepository(Repository[PartCategory]):
    def __init__(self, session: _SessionProtocol) -> None:
        super().__init__(
            session,
            kind="part category",
            table="part_categories",
            key="id",
            order="id",
            factory=lambda row: PartCategory(row["id"], row["name"]),
        )


class ElementsRepository(Repository[Element]):
    def __init__(self, session: _SessionProtocol) -> None:
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

    async def for_part_color(self, part_num: str, color_id: int) -> tuple[Element, ...]:
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                "SELECT * FROM elements WHERE part_num=? AND color_id=? "
                "ORDER BY element_id",
                (part_num, color_id),
            )
        ).fetchall()
        return tuple(self._factory(row) for row in rows)


class InventoriesRepository:
    def __init__(self, session: _SessionProtocol) -> None:
        self._session = session

    async def versions(self, owner_num: str) -> tuple[InventoryVersion, ...]:
        connection = await self._session._connection()
        rows = await (
            await connection.execute(
                """
                SELECT i.id, i.owner_num, i.version,
                       CASE WHEN li.id IS NULL THEN 0 ELSE 1 END AS is_latest
                FROM inventories i
                LEFT JOIN latest_inventories li ON li.id=i.id
                WHERE i.owner_num=? ORDER BY i.version, i.id
                """,
                (owner_num,),
            )
        ).fetchall()
        if not rows:
            raise InventoryNotFoundError("inventory", owner_num)
        return tuple(
            InventoryVersion(
                int(row["id"]),
                str(row["owner_num"]),
                int(row["version"]),
                bool(row["is_latest"]),
            )
            for row in rows
        )

    async def get(self, owner_num: str, *, version: int | None = None) -> Inventory:
        return await load_inventory(
            await self._session._connection(), owner_num, version=version
        )

    async def diff(
        self, owner_num: str, before_version: int, after_version: int
    ) -> InventoryDiff:
        before = await self.get(owner_num, version=before_version)
        after = await self.get(owner_num, version=after_version)

        def quantities(
            inventory: Inventory,
        ) -> dict[tuple[str, int], tuple[Part, Color, int, int]]:
            result: dict[tuple[str, int], tuple[Part, Color, int, int]] = {}
            for item in inventory.parts:
                key = (item.part.part_num, item.color.id)
                part, color, regular, spare = result.get(
                    key, (item.part, item.color, 0, 0)
                )
                if item.is_spare:
                    spare += item.quantity
                else:
                    regular += item.quantity
                result[key] = (part, color, regular, spare)
            return result

        before_rows = quantities(before)
        after_rows = quantities(after)
        rows: list[InventoryDiffRow] = []
        for key in sorted(before_rows.keys() | after_rows.keys()):
            record = after_rows.get(key) or before_rows[key]
            old = before_rows.get(key, (record[0], record[1], 0, 0))
            new = after_rows.get(key, (record[0], record[1], 0, 0))
            if old[2:] != new[2:]:
                rows.append(
                    InventoryDiffRow(
                        record[0], record[1], old[2], new[2], old[3], new[3]
                    )
                )
        return InventoryDiff(owner_num, before.version, after.version, tuple(rows))
