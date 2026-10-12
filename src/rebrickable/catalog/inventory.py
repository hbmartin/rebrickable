"""Newest-version structured inventories and recursive BOM expansion."""

from __future__ import annotations

from collections import defaultdict

import aiosqlite

from rebrickable.catalog.models import (
    BomContribution,
    BomRow,
    CatalogBom,
    Color,
    Inventory,
    InventoryMinifig,
    InventoryPart,
    InventorySet,
    Minifig,
    Part,
    Set,
    SkippedInventory,
)
from rebrickable.errors import InventoryCycleError, InventoryNotFoundError


def _part(row: aiosqlite.Row) -> Part:
    return Part(
        part_num=row["part_num"],
        name=row["part_name"],
        category_id=row["part_cat_id"],
        material=row["material"],
    )


def _color(row: aiosqlite.Row) -> Color:
    if row["color_name"] is None:
        return Color(
            id=row["color_id"],
            name="Unknown",
            rgb="000000",
            is_trans=False,
            num_parts=0,
            num_sets=0,
            year_from=None,
            year_to=None,
        )
    return Color(
        id=row["color_id"],
        name=row["color_name"],
        rgb=row["rgb"],
        is_trans=bool(row["is_trans"]),
        num_parts=row["color_num_parts"],
        num_sets=row["color_num_sets"],
        year_from=row["year_from"],
        year_to=row["year_to"],
    )


async def load_inventory(
    connection: aiosqlite.Connection,
    owner_num: str,
    *,
    version: int | None = None,
) -> Inventory:
    source = "latest_inventories" if version is None else "inventories"
    version_clause = "" if version is None else " AND version=?"
    values: tuple[str | int, ...] = (
        (owner_num,) if version is None else (owner_num, version)
    )
    row = await (
        await connection.execute(
            f"SELECT id, version FROM {source} WHERE owner_num=?{version_clause} "  # noqa: S608 - internal SQL clauses/identifiers; values are bound separately
            "ORDER BY id DESC LIMIT 1",
            values,
        )
    ).fetchone()
    if row is None:
        identifier = owner_num if version is None else f"{owner_num} version {version}"
        raise InventoryNotFoundError("inventory", identifier)
    inventory_id, version = int(row["id"]), int(row["version"])
    part_rows = await (
        await connection.execute(
            """
            SELECT ip.part_num, p.name AS part_name, p.part_cat_id, p.material,
                   ip.color_id, c.name AS color_name, c.rgb, c.is_trans,
                   c.num_parts AS color_num_parts, c.num_sets AS color_num_sets,
                   c.year_from, c.year_to, ip.quantity, ip.is_spare, ip.image_url
            FROM inventory_parts ip
            JOIN parts p ON p.part_num=ip.part_num
            LEFT JOIN colors c ON c.id=ip.color_id
            WHERE ip.inventory_id=?
            ORDER BY ip.part_num, ip.color_id, ip.is_spare
            """,
            (inventory_id,),
        )
    ).fetchall()
    element_rows = await (
        await connection.execute(
            """
            SELECT DISTINCT e.part_num, e.color_id, e.element_id
            FROM elements e
            JOIN inventory_parts ip
              ON ip.part_num = e.part_num AND ip.color_id = e.color_id
            WHERE ip.inventory_id=?
            ORDER BY e.part_num, e.color_id, e.element_id
            """,
            (inventory_id,),
        )
    ).fetchall()
    elements_by_key: defaultdict[tuple[str, int], list[str]] = defaultdict(list)
    for element_row in element_rows:
        elements_by_key[(element_row["part_num"], element_row["color_id"])].append(
            str(element_row["element_id"])
        )
    parts = [
        InventoryPart(
            part=_part(part_row),
            color=_color(part_row),
            quantity=int(part_row["quantity"]),
            is_spare=bool(part_row["is_spare"]),
            element_ids=tuple(
                elements_by_key.get((part_row["part_num"], part_row["color_id"]), ())
            ),
            image_url=part_row["image_url"],
        )
        for part_row in part_rows
    ]
    set_rows = await (
        await connection.execute(
            """
            SELECT s.set_num, s.name, s.year, s.theme_id, s.num_parts, s.image_url, x.quantity
            FROM inventory_sets x JOIN sets s ON s.set_num=x.set_num
            WHERE x.inventory_id=? ORDER BY s.set_num
            """,
            (inventory_id,),
        )
    ).fetchall()
    sets = tuple(
        InventorySet(
            set=Set(
                set_num=item["set_num"],
                name=item["name"],
                year=item["year"],
                theme_id=item["theme_id"],
                num_parts=item["num_parts"],
                image_url=item["image_url"],
            ),
            quantity=item["quantity"],
        )
        for item in set_rows
    )
    fig_rows = await (
        await connection.execute(
            """
            SELECT f.fig_num, f.name, f.num_parts, f.image_url, x.quantity
            FROM inventory_minifigs x JOIN minifigs f ON f.fig_num=x.fig_num
            WHERE x.inventory_id=? ORDER BY f.fig_num
            """,
            (inventory_id,),
        )
    ).fetchall()
    minifigs = tuple(
        InventoryMinifig(
            minifig=Minifig(
                fig_num=item["fig_num"],
                name=item["name"],
                num_parts=item["num_parts"],
                image_url=item["image_url"],
            ),
            quantity=item["quantity"],
        )
        for item in fig_rows
    )
    return Inventory(
        owner_num=owner_num,
        version=version,
        parts=tuple(parts),
        sets=sets,
        minifigs=minifigs,
    )


async def bill_of_materials(
    connection: aiosqlite.Connection,
    owner_num: str,
    *,
    include_spares: bool = False,
    strict: bool = False,
) -> CatalogBom:
    quantities: defaultdict[tuple[str, int], int] = defaultdict(int)
    entities: dict[tuple[str, int], tuple[Part, Color]] = {}
    inventory_cache: dict[str, Inventory] = {}
    skipped: list[SkippedInventory] = []
    contributions: defaultdict[tuple[str, int], list[BomContribution]] = defaultdict(
        list
    )

    async def expand(owner: str, multiplier: int, path: tuple[str, ...]) -> None:
        if owner in path:
            raise InventoryCycleError((*path, owner))
        current_path = (*path, owner)
        inventory = inventory_cache.get(owner)
        if inventory is None:
            try:
                inventory = await load_inventory(connection, owner)
            except InventoryNotFoundError as exc:
                if strict or not path:
                    raise
                skipped.append(
                    SkippedInventory(
                        owner_num=owner, owner_path=current_path, reason=str(exc)
                    )
                )
                return
            inventory_cache[owner] = inventory
        for item in inventory.parts:
            if item.is_spare and not include_spares:
                continue
            key = (item.part.part_num, item.color.id)
            amount = multiplier * item.quantity
            quantities[key] += amount
            entities[key] = (item.part, item.color)
            contributions[key].append(
                BomContribution(quantity=amount, owner_path=current_path)
            )
        for item in inventory.sets:
            await expand(item.set.set_num, multiplier * item.quantity, current_path)
        for item in inventory.minifigs:
            await expand(item.minifig.fig_num, multiplier * item.quantity, current_path)

    await expand(owner_num, 1, ())
    rows = tuple(
        BomRow(
            part=entities[key][0],
            color=entities[key][1],
            quantity=quantity,
            provenance=tuple(contributions[key]),
        )
        for key, quantity in sorted(quantities.items())
    )
    return CatalogBom(rows=rows, skipped=tuple(skipped))
