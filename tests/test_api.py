from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

import pytest

from rebrickable.api.client import RebrickableClient
from rebrickable.api.models import CreateUserTokenRequest
from rebrickable.api.operation_registry import OPENAPI_SHA256, OPERATIONS
from rebrickable.config import Config
from rebrickable.data import OPENAPI_RESOURCE
from rebrickable.errors import ApiAuthenticationError, UnsafePaginationUrlError


@dataclass
class FakeResponse:
    payload: Any
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "" if self.status_code == 204 else json.dumps(self.payload)

    def json(self) -> Any:
        return self.payload


class FakeTransport:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method, url, *, headers, params=None, data=None):
        self.requests.append(
            (method, url, {"headers": headers, "params": params, "data": data})
        )
        return self.responses.pop(0)


def client(transport: FakeTransport) -> RebrickableClient:
    return RebrickableClient(
        api_key="very-secret",
        user_token="user-secret",
        config=Config(request_interval=0, max_retries=0),
        transport=transport,
    )


@pytest.mark.asyncio
async def test_list_get_auth_and_extra_fields() -> None:
    transport = FakeTransport(
        FakeResponse(
            {
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"id": 4, "name": "Red", "new_field": 1}],
            }
        ),
        FakeResponse({"part_num": "3001", "name": "Brick"}),
    )
    api = client(transport)
    page = await api.list_colors(page_size=1000)
    part = await api.get_part("3001")
    assert page.results[0].extra["new_field"] == 1
    assert part.part_num == "3001"
    assert transport.requests[0][2]["headers"]["Authorization"] == "key very-secret"
    assert "very-secret" not in transport.requests[0][1]
    assert "very-secret" not in repr(api)


@pytest.mark.asyncio
async def test_iterator_rejects_unsafe_next_url() -> None:
    transport = FakeTransport(
        FakeResponse(
            {
                "count": 1,
                "next": "https://evil.test/api/v3/lego/colors/?page=2",
                "previous": None,
                "results": [{"id": 4, "name": "Red"}],
            }
        ),
    )
    api = client(transport)
    iterator = api.iter_colors()
    assert (await anext(iterator)).id == 4
    with pytest.raises(UnsafePaginationUrlError):
        await anext(iterator)


@pytest.mark.asyncio
async def test_error_redaction_and_form_encoding() -> None:
    transport = FakeTransport(
        FakeResponse({"detail": "bad key"}, status_code=401),
        FakeResponse({"user_token": "created-token"}),
    )
    api = client(transport)
    with pytest.raises(ApiAuthenticationError) as captured:
        await api.get_part("3001")
    assert "very-secret" not in str(captured.value)
    token = await api.create_user_token(
        CreateUserTokenRequest(username="u", password="p")
    )
    assert token.user_token == "created-token"
    assert transport.requests[-1][2]["data"] == {"username": "u", "password": "p"}


def test_openapi_parity_and_public_methods() -> None:
    assert len(OPERATIONS) == 63
    assert (
        OPENAPI_SHA256
        == "91b49e310f8fb2db4ff7474e2775921897e10319a71ec053cac61f3a40fa7cb6"
    )
    expected = {
        "lego_colors_list": "list_colors",
        "lego_colors_read": "get_color",
        "lego_elements_read": "get_element",
        "lego_minifigs_list": "list_minifigs",
        "lego_minifigs_read": "get_minifig",
        "lego_minifigs_parts_list": "list_minifig_parts",
        "lego_minifigs_sets_list": "list_minifig_sets",
        "lego_part_categories_list": "list_part_categories",
        "lego_part_categories_read": "get_part_category",
        "lego_parts_list": "list_parts",
        "lego_parts_read": "get_part",
        "lego_parts_colors_list": "list_part_colors",
        "lego_parts_colors_read": "get_part_color",
        "lego_parts_colors_sets_list": "list_part_color_sets",
        "lego_sets_list": "list_sets",
        "lego_sets_read": "get_set",
        "lego_sets_alternates_list": "list_set_alternates",
        "lego_sets_minifigs_list": "list_set_minifigs",
        "lego_sets_parts_list": "list_set_parts",
        "lego_sets_sets_list": "list_set_sets",
        "lego_themes_list": "list_themes",
        "lego_themes_read": "get_theme",
        "swagger_list": "get_openapi_spec",
        "users__token_create": "create_user_token",
        "users_badges_list": "list_badges",
        "users_badges_read": "get_badge",
        "users_allparts_list": "list_user_all_parts",
        "users_build_read": "get_user_build_requirements",
        "users_lost_parts_list": "list_user_lost_parts",
        "users_lost_parts_create": "add_user_lost_parts",
        "users_lost_parts_delete": "delete_user_lost_part",
        "users_minifigs_list": "list_user_minifigs",
        "users_parts_list": "list_user_parts",
        "users_profile_read": "get_user_profile",
        "users_partlists_list": "list_user_part_lists",
        "users_partlists_create": "create_user_part_list",
        "users_partlists_read": "get_user_part_list",
        "users_partlists_update": "replace_user_part_list",
        "users_partlists_partial_update": "update_user_part_list",
        "users_partlists_delete": "delete_user_part_list",
        "users_partlists_parts_list": "list_user_part_list_parts",
        "users_partlists_parts_create": "add_user_part_list_parts",
        "users_partlists_parts_read": "get_user_part_list_part",
        "users_partlists_parts_update": "replace_user_part_list_part",
        "users_partlists_parts_delete": "delete_user_part_list_part",
        "users_setlists_list": "list_user_set_lists",
        "users_setlists_create": "create_user_set_list",
        "users_setlists_read": "get_user_set_list",
        "users_setlists_update": "replace_user_set_list",
        "users_setlists_partial_update": "update_user_set_list",
        "users_setlists_delete": "delete_user_set_list",
        "users_setlists_sets_list": "list_user_set_list_sets",
        "users_setlists_sets_create": "add_user_set_list_sets",
        "users_setlists_sets_read": "get_user_set_list_set",
        "users_setlists_sets_update": "replace_user_set_list_set",
        "users_setlists_sets_partial_update": "update_user_set_list_set",
        "users_setlists_sets_delete": "delete_user_set_list_set",
        "users_sets_list": "list_user_sets",
        "users_sets_create": "add_user_sets",
        "users_sets_sync_create": "sync_user_sets",
        "users_sets_read": "get_user_set",
        "users_sets_update": "set_user_set_quantity",
        "users_sets_delete": "delete_user_set",
    }
    assert set(expected) == set(OPERATIONS)
    assert all(hasattr(RebrickableClient, name) for name in expected.values())
    document = json.loads(
        files("rebrickable.data").joinpath(OPENAPI_RESOURCE).read_text(encoding="utf-8")
    )
    operation_ids = {
        operation["operationId"]
        for path in document["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert operation_ids == set(OPERATIONS)
