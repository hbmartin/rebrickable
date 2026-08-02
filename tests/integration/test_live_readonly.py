from __future__ import annotations

import os

import pytest

from rebrickable import RebrickableClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.enable_socket,
    pytest.mark.skipif(
        os.environ.get("REBRICKABLE_RUN_INTEGRATION") != "1",
        reason="live integration tests require an explicit guard",
    ),
]


@pytest.mark.asyncio
async def test_live_read_only_catalog_contract() -> None:
    key = os.environ.get("REBRICKABLE_API_KEY")
    if not key:
        pytest.skip("REBRICKABLE_API_KEY is not configured")
    async with RebrickableClient(api_key=key) as client:
        color = await client.get_color(4)
        part = await client.get_part("3001")
        page = await client.list_sets(search="Galaxy Explorer", page_size=1)
    assert color.id == 4
    assert part.part_num == "3001"
    assert page.count >= len(page.results)
