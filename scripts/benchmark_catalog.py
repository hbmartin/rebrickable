"""Measure representative warm catalog operations and enforce generous ceilings."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from statistics import median
from time import perf_counter

from rebrickable import Bom, BomItem, ColorRef, PartRef, RebrickableSession, SearchKind


async def sample(args: argparse.Namespace) -> dict[str, float | int | str]:
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
                part=PartRef(system="rebrickable", value=row.part.part_num),
                color=ColorRef(system="rebrickable", value=row.color.id),
                quantity=row.quantity,
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
    return results


async def benchmark(args: argparse.Namespace) -> dict[str, object]:
    await sample(args)  # Warm filesystem and SQLite caches before the five samples.
    samples = [await sample(args) for _ in range(5)]
    medians = {
        name: median(float(item[name]) for item in samples)
        for name in (
            "connect_seconds",
            "warm_part_seconds",
            "warm_search_seconds",
            "validation_seconds",
            "validation_seconds_per_row",
        )
    }
    async with await RebrickableSession.connect() as session:
        state = await session.opened_catalog_state()
    results: dict[str, object] = {
        "samples": samples,
        "medians": medians,
        "snapshot": {
            "snapshot_id": state.snapshot_id,
            "schema_version": state.schema_version,
            "retrieved_at": state.retrieved_at.isoformat()
            if state.retrieved_at
            else None,
            "files": [asdict(item) for item in state.files],
        },
        "peak_rss_mib": peak_rss_mib(),
    }
    ceilings = {
        "connect_seconds": args.max_connect_seconds,
        "warm_part_seconds": args.max_part_seconds,
        "warm_search_seconds": args.max_search_seconds,
        "validation_seconds_per_row": args.max_validation_seconds_per_row,
    }
    failures = [
        f"{name}={medians[name]:.6f} exceeds {ceiling:.6f}"
        for name, ceiling in ceilings.items()
        if medians[name] > ceiling
    ]
    if (rss := results["peak_rss_mib"]) is not None and float(rss) > args.max_rss_mib:
        failures.append(f"RSS {rss} MiB exceeds {args.max_rss_mib} MiB")
    print(json.dumps(results, sort_keys=True))
    if failures:
        raise RuntimeError("; ".join(failures))
    return results


def peak_rss_mib() -> float | None:
    if sys.platform == "win32":
        return None
    import resource  # noqa: PLC0415 - resource is unavailable on Windows

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024 if sys.platform == "darwin" else 1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rss-mib", type=float, default=512)
    parser.add_argument("--validation-rows", type=int, default=300)
    parser.add_argument("--max-connect-seconds", type=float, default=5.0)
    parser.add_argument("--max-part-seconds", type=float, default=0.05)
    parser.add_argument("--max-search-seconds", type=float, default=1.0)
    parser.add_argument("--max-validation-seconds-per-row", type=float, default=0.03)
    asyncio.run(benchmark(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
