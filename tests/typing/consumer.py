"""Statically checked public consumer contracts; these examples make no calls."""

from typing import assert_type

from rebrickable import RebrickableClient, RebrickableSession
from rebrickable.api.models import ApiColor, ApiPage, ApiPart
from rebrickable.api.query_types import LegoPartsListQuery
from rebrickable.catalog.models import Part


async def consumer(session: RebrickableSession, client: RebrickableClient) -> None:
    assert_type(await session.parts.get("3001"), Part | None)
    assert_type(await session.parts.require("3001"), Part)
    async for part in session.parts.iter(page_size=10):
        assert_type(part, Part)
    query: LegoPartsListQuery = {"search": "brick", "inc_part_details": True}
    assert_type(await client.get_part("3001"), ApiPart)
    assert_type(await client.list_colors(page_size=10), ApiPage[ApiColor])
    async for api_part in client.iter_parts(**query):
        assert_type(api_part, ApiPart)
