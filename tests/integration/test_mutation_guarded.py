from __future__ import annotations

import os

import pytest

from rebrickable import RebrickableClient
from rebrickable.api.models import PartListRequest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mutation,
    pytest.mark.skipif(
        os.environ.get("REBRICKABLE_RUN_MUTATIONS") != "I_UNDERSTAND",
        reason="mutation round trip requires the destructive guard",
    ),
]


@pytest.mark.asyncio
async def test_disposable_user_part_list_round_trip() -> None:
    key = os.environ.get("REBRICKABLE_API_KEY")
    token = os.environ.get("REBRICKABLE_DISPOSABLE_USER_TOKEN")
    if not key or not token:
        pytest.skip("disposable API key and user token are required")
    async with RebrickableClient(api_key=key, user_token=token) as client:
        created = await client.create_user_part_list(
            PartListRequest(name="rebrickable integration cleanup", is_buildable=False)
        )
        try:
            fetched = await client.get_user_part_list(created.id)
            assert fetched.id == created.id
        finally:
            await client.delete_user_part_list(created.id)
