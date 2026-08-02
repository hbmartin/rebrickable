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
        sets = await client.list_sets(search="Galaxy Explorer", page_size=1)
        categories = await client.list_part_categories(page_size=1)
        themes = await client.list_themes(page_size=1)
        element = await client.get_element("300121")
        part_colors = await client.list_part_colors("3001", page_size=1)
        set_parts = await client.list_set_parts("75192-1", page_size=1)
    assert color.id == 4
    assert part.part_num == "3001"
    assert sets.count >= len(sets.results)
    assert categories.count >= len(categories.results)
    assert themes.count >= len(themes.results)
    assert element.element_id == "300121"
    assert part_colors.count >= len(part_colors.results)
    assert set_parts.count >= len(set_parts.results)
