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
    return Part(row["part_num"], row["part_name"], row["part_cat_id"], row["material"])


def _color(row: aiosqlite.Row) -> Color:
    if row["color_name"] is None:
        return Color(row["color_id"], "Unknown", "000000", False, 0, 0, None, None)
    return Color(
        row["color_id"],
        row["color_name"],
        row["rgb"],
        bool(row["is_trans"]),
        row["color_num_parts"],
        row["color_num_sets"],
        row["year_from"],
        row["year_to"],
    )


async def load_inventory(connection: aiosqlite.Connection, owner_num: str) -> Inventory:
    row = await (
        await connection.execute(
            "SELECT id, version FROM latest_inventories WHERE owner_num=? ORDER BY id DESC LIMIT 1",
            (owner_num,),
        )
    ).fetchone()
    if row is None:
        raise InventoryNotFoundError("inventory", owner_num)
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
            _part(part_row),
            _color(part_row),
            int(part_row["quantity"]),
            bool(part_row["is_spare"]),
            tuple(
                elements_by_key.get((part_row["part_num"], part_row["color_id"]), ())
            ),
            part_row["image_url"],
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
            Set(
                item["set_num"],
                item["name"],
                item["year"],
                item["theme_id"],
                item["num_parts"],
                item["image_url"],
            ),
            item["quantity"],
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
            Minifig(
                item["fig_num"], item["name"], item["num_parts"], item["image_url"]
            ),
            item["quantity"],
        )
        for item in fig_rows
    )
    return Inventory(owner_num, version, tuple(parts), sets, minifigs)


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
                skipped.append(SkippedInventory(owner, current_path, str(exc)))
                return
            inventory_cache[owner] = inventory
        for item in inventory.parts:
            if item.is_spare and not include_spares:
                continue
            key = (item.part.part_num, item.color.id)
            amount = multiplier * item.quantity
            quantities[key] += amount
            entities[key] = (item.part, item.color)
            contributions[key].append(BomContribution(amount, current_path))
        for item in inventory.sets:
            await expand(item.set.set_num, multiplier * item.quantity, current_path)
        for item in inventory.minifigs:
            await expand(item.minifig.fig_num, multiplier * item.quantity, current_path)

    await expand(owner_num, 1, ())
    rows = tuple(
        BomRow(
            entities[key][0],
            entities[key][1],
            quantity,
            tuple(contributions[key]),
        )
        for key, quantity in sorted(quantities.items())
    )
    return CatalogBom(rows, tuple(skipped))
