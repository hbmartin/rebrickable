from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rebrickable import Bom, BomItem, ColorRef, PartRef, RebrickableSession
from rebrickable.api.pagination import validate_next_url
from rebrickable.errors import (
    InventoryCycleError,
    PaginationCycleError,
    UnsafePaginationUrlError,
)
from rebrickable.exports import escape_csv_formula
from tests.catalog_fixture import ROWS, build_catalog_config

ITEMS = st.lists(
    st.tuples(
        st.sampled_from(["3001", "3002", "0001"]),
        st.integers(0, 10),
        st.integers(1, 100),
    ),
    max_size=30,
)


@given(ITEMS)
def test_bom_normalization_and_csv_round_trip(rows: list[tuple[str, int, int]]) -> None:
    items = [
        BomItem(
            part=PartRef(system="rebrickable", value=part),
            color=ColorRef(system="rebrickable", value=color),
            quantity=quantity,
        )
        for part, color, quantity in rows
    ]
    bom = Bom.normalize(items)
    assert Bom.normalize(list(reversed(items))) == bom
    assert Bom.normalize(list(bom.items)).items == bom.items
    assert bom.total_quantity == sum(quantity for _, _, quantity in rows)
    assert Bom.from_rebrickable_csv(bom.to_rebrickable_csv()).items == bom.items
    occurrences = Counter((part, color) for part, color, _ in rows)
    assert {
        (row.part.value, row.color.value): row.occurrences for row in bom.duplicate_rows
    } == {key: count for key, count in occurrences.items() if count > 1}
    assert bom.diff(bom).rows == ()


@given(st.sampled_from(["=", "+", "-", "@", "\t", "\r"]), st.text(max_size=100))
def test_formula_escaping(prefix: str, suffix: str) -> None:
    value = prefix + suffix
    escaped = escape_csv_formula(value)
    assert escaped == "'" + value
    assert escape_csv_formula(escaped) == escaped


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=30),
    st.sampled_from(["http", "https"]),
)
def test_pagination_cannot_leave_origin(host: str, scheme: str) -> None:
    url = f"{scheme}://{host}.invalid/api/v3/lego/parts/?page=2"
    with pytest.raises(UnsafePaginationUrlError):
        validate_next_url(url, "https://rebrickable.com/api/v3")
    assert validate_next_url(
        "https://rebrickable.com/api/v3/lego/parts/?page=2",
        "https://rebrickable.com/api/v3",
    ).endswith("page=2")


@given(st.lists(st.integers(1, 5), min_size=1, max_size=5))
def test_recursive_inventory_conserves_quantities(multipliers: list[int]) -> None:
    async def check(root: Path) -> None:
        rows = dict(ROWS)
        count = len(multipliers) + 1
        rows["sets"] = [(f"{i}-1", f"Owner {i}", 2024, 1, 1, "") for i in range(count)]
        rows["inventories"] = [(i + 1, 1, f"{i}-1") for i in range(count)]
        rows["inventory_parts"] = [
            (i + 1, "3001", 4, i + 1, "False", "") for i in range(count)
        ]
        rows["inventory_sets"] = [
            (i + 1, f"{i + 1}-1", value) for i, value in enumerate(multipliers)
        ]
        rows["inventory_minifigs"] = []
        config = build_catalog_config(root, rows)
        async with await RebrickableSession.connect(config) as session:
            bom = await session.sets.bill_of_materials("0-1")
        expected, multiplier = 1, 1
        for i, value in enumerate(multipliers, start=2):
            multiplier *= value
            expected += i * multiplier
        assert len(bom.rows) == 1
        assert bom.rows[0].quantity == expected
        assert sum(item.quantity for item in bom.rows[0].provenance) == expected
        rows["inventory_sets"].append((count, "0-1", 1))
        cyclic = build_catalog_config(root / "cyclic", rows)
        async with await RebrickableSession.connect(cyclic) as session:
            with pytest.raises(InventoryCycleError) as failure:
                await session.sets.bill_of_materials("0-1")
        assert failure.value.path == (*(f"{i}-1" for i in range(count)), "0-1")

    with tempfile.TemporaryDirectory(prefix="rebrickable-property-") as directory:
        asyncio.run(check(Path(directory)))


@given(st.text(max_size=100), st.integers(-10000, 10000))
def test_dto_json_round_trip(name: str, upstream_field: int) -> None:
    from rebrickable.api.models import ApiPart  # noqa: PLC0415 - property target

    model = ApiPart.model_validate(
        {"part_num": "3001", "name": name, "future_field": upstream_field}
    )
    assert ApiPart.model_validate_json(model.model_dump_json()) == model
    assert model.extra["future_field"] == upstream_field


@given(st.integers(2, 8))
def test_adversarial_pagination_cycle_is_bounded(pages: int) -> None:
    from tests.test_api import (  # noqa: PLC0415 - local transport fixture
        FakeResponse,
        FakeTransport,
        client,
    )

    async def check() -> None:
        transport = FakeTransport(
            *(
                FakeResponse(
                    payload={
                        "count": pages,
                        "previous": None,
                        "next": f"https://rebrickable.com/api/v3/lego/parts/?page={i + 2 if i + 1 < pages else 2}",
                        "results": [{"part_num": str(i), "name": "Fixture"}],
                    }
                )
                for i in range(pages)
            )
        )
        values: list[str] = []
        async with client(transport) as api:

            async def consume() -> None:
                async for part in api.iter_parts(page_size=1):
                    values.append(part.part_num)

            with pytest.raises(PaginationCycleError):
                await consume()
        assert values == [str(i) for i in range(pages)]
        assert len(transport.requests) == pages

    asyncio.run(check())
