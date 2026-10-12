"""Offline synthetic catalog memory and batching regression gate."""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
import tracemalloc
from collections.abc import Iterable
from pathlib import Path

from rebrickable import Bom, BomItem, ColorRef, PartRef, RebrickableSession
from rebrickable.catalog.importers import import_catalog
from tests.catalog_fixture import ROWS, build_catalog_config, write_dataset

MIB = 1024 * 1024


def peak_rss_mib() -> float | None:
    if sys.platform != "linux":
        return None
    import resource  # noqa: PLC0415 - unavailable on Windows

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def synthetic_rows(count: int) -> dict[str, Iterable[tuple[object, ...]]]:
    rows: dict[str, Iterable[tuple[object, ...]]] = dict(ROWS)
    owners = count // 10000
    rows.update(
        {
            "parts": ((f"p{i:05}", f"Brick {i}", 1, "Plastic") for i in range(10000)),
            "elements": [],
            "part_relationships": [],
            "sets": [
                (f"{i}-1", f"Set {i}", 2024, 1, 10000, "") for i in range(1, owners + 1)
            ],
            "inventories": [(i, 1, f"{i}-1") for i in range(1, owners + 1)],
            "inventory_parts": (
                (i // 10000 + 1, f"p{i % 10000:05}", 4, 1, "False", "")
                for i in range(count)
            ),
            "inventory_sets": [],
            "inventory_minifigs": [],
        }
    )
    return rows


def measure_import(root: Path, count: int) -> float:
    root.mkdir()
    sources = {
        name: write_dataset(root, name, rows)
        for name, rows in synthetic_rows(count).items()
    }
    tracemalloc.start()
    try:
        counts, _ = import_catalog(
            sources,
            root / "catalog.sqlite",
            snapshot_id="synthetic",
            retrieved_at="2026-10-12T00:00:00+00:00",
        )
        peak = tracemalloc.get_traced_memory()[1] / MIB
    finally:
        tracemalloc.stop()
    if counts["inventory_parts"] != count or counts["parts"] != 10000:
        raise AssertionError("synthetic import lost rows")
    if peak > 32:
        raise AssertionError(f"Python allocation peak {peak:.2f} MiB exceeds 32 MiB")
    return peak


async def check_batching(root: Path) -> dict[int, int]:
    config = build_catalog_config(root, synthetic_rows(10000))
    results: dict[int, int] = {}
    async with await RebrickableSession.connect(config) as session:
        connection = await session._connection()
        for count in (299, 300, 301, 1200):
            statements: list[str] = []
            await connection.set_trace_callback(statements.append)
            bom = Bom.normalize(
                [
                    BomItem(
                        part=PartRef(system="rebrickable", value=f"p{i:05}"),
                        color=ColorRef(system="rebrickable", value=4),
                        quantity=1,
                    )
                    for i in range(count)
                ]
            )
            report = await session.validate_bom(bom)
            await connection.set_trace_callback(None)
            scans = sum("COUNT(DISTINCT s.set_num)" in sql for sql in statements)
            if (
                report.exact_count != count
                or report.total_quantity != count
                or len(report.rows) != count
                or any(
                    row.item != item
                    or row.rebrickable_part != item.part.value
                    or row.rebrickable_color != 4
                    or not row.available
                    for item, row in zip(bom.items, report.rows, strict=True)
                )
                or scans != math.ceil(count / 300)
            ):
                raise AssertionError(
                    f"incorrect evidence batching for {count} rows: {scans} scans"
                )
            results[count] = scans
    return results


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rebrickable-bounds-") as directory:
        root = Path(directory)
        peaks = [measure_import(root / str(count), count) for count in (10000, 100000)]
        if peaks[1] - peaks[0] > 8:
            raise AssertionError("import Python allocation growth exceeds 8 MiB")
        scans = asyncio.run(check_batching(root / "queries"))
        rss = peak_rss_mib()
        if rss is not None and rss > 512:
            raise AssertionError(f"process peak RSS {rss:.2f} MiB exceeds 512 MiB")
        print(
            json.dumps(
                {
                    "python_peak_mib": peaks,
                    "peak_rss_mib": rss,
                    "evidence_scans": scans,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
