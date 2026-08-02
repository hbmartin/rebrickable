"""Measure representative warm catalog operations and enforce generous ceilings."""

from __future__ import annotations

import argparse
import asyncio
import json
from time import perf_counter

from rebrickable import Bom, BomItem, ColorRef, PartRef, RebrickableSession, SearchKind


async def benchmark(args: argparse.Namespace) -> dict[str, float | int | str]:
    started = perf_counter()
    async with await RebrickableSession.connect() as session:
        connect_seconds = perf_counter() - started
        parts = await session.parts.list(limit=1)
        if not parts:
            raise RuntimeError("catalog contains no parts")
        part_num = parts[0].part_num
        await session.parts.get(part_num)
        started = perf_counter()
        await session.parts.get(part_num)
        part_seconds = perf_counter() - started

        await session.search("brick", kinds={SearchKind.PART}, limit=50)
        started = perf_counter()
        await session.search("brick", kinds={SearchKind.PART}, limit=50)
        search_seconds = perf_counter() - started

        sets = await session.sets.list(limit=1_000)
        if not sets:
            raise RuntimeError("catalog contains no sets")
        owner_num = max(sets, key=lambda item: item.num_parts).set_num
        catalog_bom = await session.sets.bill_of_materials(owner_num)
        items = tuple(
            BomItem(
                PartRef("rebrickable", row.part.part_num),
                ColorRef("rebrickable", row.color.id),
                row.quantity,
            )
            for row in catalog_bom.rows[: args.validation_rows]
        )
        validation_seconds = 0.0
        if items:
            started = perf_counter()
            await session.validate_bom(Bom.normalize(items))
            validation_seconds = perf_counter() - started
        validation_per_row = validation_seconds / max(1, len(items))
        state = await session.opened_catalog_state()

    results: dict[str, float | int | str] = {
        "snapshot_id": state.snapshot_id or "unknown",
        "connect_seconds": connect_seconds,
        "warm_part_seconds": part_seconds,
        "warm_search_seconds": search_seconds,
        "validation_rows": len(items),
        "validation_seconds": validation_seconds,
        "validation_seconds_per_row": validation_per_row,
    }
    ceilings = {
        "connect_seconds": args.max_connect_seconds,
        "warm_part_seconds": args.max_part_seconds,
        "warm_search_seconds": args.max_search_seconds,
        "validation_seconds_per_row": args.max_validation_seconds_per_row,
    }
    failures = [
        f"{name}={results[name]:.6f} exceeds {ceiling:.6f}"
        for name, ceiling in ceilings.items()
        if float(results[name]) > ceiling
    ]
    print(json.dumps(results, sort_keys=True))
    if failures:
        raise RuntimeError("; ".join(failures))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validation-rows", type=int, default=300)
    parser.add_argument("--max-connect-seconds", type=float, default=5.0)
    parser.add_argument("--max-part-seconds", type=float, default=0.05)
    parser.add_argument("--max-search-seconds", type=float, default=1.0)
    parser.add_argument("--max-validation-seconds-per-row", type=float, default=0.03)
    asyncio.run(benchmark(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
