"""Drive every registry operation through the real ``_send``.

Each public wrapper is invoked against a fake transport and the captured
request is validated against the operation registry: HTTP method, fully
substituted path, and query/form/JSON payloads that the recorded contract
actually accepts. This is the net that catches wrapper/registry drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from rebrickable.api.models import (
    CreateUserTokenRequest,
    LostPartRequest,
    PartListPartRequest,
    PartListRequest,
    PartListUpdateRequest,
    QuantityRequest,
    SetListRequest,
    SetListSetUpdateRequest,
    SetListUpdateRequest,
    SetQuantityRequest,
    UserSetsSyncRequest,
)
from rebrickable.api.operation_registry import OPERATIONS, Operation

from .test_api import FakeResponse, FakeTransport, client

_UNSET = object()
_PAGE = {"count": 0, "next": None, "previous": None, "results": []}


@dataclass(frozen=True)
class Case:
    operation_id: str
    method_name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    response: Any = field(default_factory=dict)
    status: int = 200
    expected_data: dict[str, Any] | None = None
    expected_json: Any = _UNSET


CASES: tuple[Case, ...] = (
    Case("lego_colors_list", "list_colors", response=_PAGE),
    Case("lego_colors_read", "get_color", (4,), response={"id": 4, "name": "Red"}),
    Case(
        "lego_elements_read",
        "get_element",
        ("300121",),
        response={"element_id": "300121"},
    ),
    Case("lego_minifigs_list", "list_minifigs", response=_PAGE),
    Case(
        "lego_minifigs_read",
        "get_minifig",
        ("fig-1",),
        response={"set_num": "fig-1", "name": "Fig"},
    ),
    Case("lego_minifigs_parts_list", "list_minifig_parts", ("fig-1",), response=_PAGE),
    Case("lego_minifigs_sets_list", "list_minifig_sets", ("fig-1",), response=_PAGE),
    Case("lego_part_categories_list", "list_part_categories", response=_PAGE),
    Case(
        "lego_part_categories_read",
        "get_part_category",
        (1,),
        response={"id": 1, "name": "Bricks"},
    ),
    Case(
        "lego_parts_list",
        "list_parts",
        kwargs={"inc_part_details": True},
        response=_PAGE,
    ),
    Case(
        "lego_parts_read",
        "get_part",
        ("3001",),
        response={"part_num": "3001", "name": "Brick"},
    ),
    Case("lego_parts_colors_list", "list_part_colors", ("3001",), response=_PAGE),
    Case("lego_parts_colors_read", "get_part_color", ("3001", 4)),
    Case(
        "lego_parts_colors_sets_list",
        "list_part_color_sets",
        ("3001", 4),
        response=_PAGE,
    ),
    Case("lego_sets_list", "list_sets", response=_PAGE),
    Case(
        "lego_sets_read", "get_set", ("1-1",), response={"set_num": "1-1", "name": "S"}
    ),
    Case("lego_sets_alternates_list", "list_set_alternates", ("1-1",), response=_PAGE),
    Case("lego_sets_minifigs_list", "list_set_minifigs", ("1-1",), response=_PAGE),
    Case("lego_sets_parts_list", "list_set_parts", ("1-1",), response=_PAGE),
    Case("lego_sets_sets_list", "list_set_sets", ("1-1",), response=_PAGE),
    Case("lego_themes_list", "list_themes", response=_PAGE),
    Case("lego_themes_read", "get_theme", (1,), response={"id": 1, "name": "Space"}),
    Case("swagger_list", "get_openapi_spec"),
    Case(
        "users__token_create",
        "create_user_token",
        (CreateUserTokenRequest(username="u", password="p"),),
        response={"user_token": "token-1"},
        expected_data={"username": "u", "password": "p"},
    ),
    Case("users_badges_list", "list_badges", response=_PAGE),
    Case("users_badges_read", "get_badge", (1,), response={"id": 1, "name": "Badge"}),
    Case("users_allparts_list", "list_user_all_parts", response=_PAGE),
    Case("users_build_read", "get_user_build_requirements", ("1-1",)),
    Case("users_lost_parts_list", "list_user_lost_parts", response=_PAGE),
    Case(
        "users_lost_parts_create",
        "add_user_lost_parts",
        (LostPartRequest(inv_part_id=1),),
        response={"id": 1},
        expected_data={"inv_part_id": 1},
    ),
    Case("users_lost_parts_delete", "delete_user_lost_part", (1,), status=204),
    Case("users_minifigs_list", "list_user_minifigs", response=_PAGE),
    Case("users_parts_list", "list_user_parts", response=_PAGE),
    Case("users_profile_read", "get_user_profile", response={"username": "u"}),
    Case("users_partlists_list", "list_user_part_lists", response=_PAGE),
    Case(
        "users_partlists_create",
        "create_user_part_list",
        (PartListRequest(name="p"),),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case(
        "users_partlists_read",
        "get_user_part_list",
        (1,),
        response={"id": 1, "name": "p"},
    ),
    Case(
        "users_partlists_update",
        "replace_user_part_list",
        (1, PartListRequest(name="p")),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case(
        "users_partlists_partial_update",
        "update_user_part_list",
        (1, PartListUpdateRequest(name="p")),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case("users_partlists_delete", "delete_user_part_list", (1,), status=204),
    Case(
        "users_partlists_parts_list", "list_user_part_list_parts", (1,), response=_PAGE
    ),
    Case(
        "users_partlists_parts_create",
        "add_user_part_list_parts",
        (1, PartListPartRequest(part_num="3001", color_id=4, quantity=1)),
        expected_data={"part_num": "3001", "color_id": 4, "quantity": 1},
    ),
    Case(
        "users_partlists_parts_read",
        "get_user_part_list_part",
        (1, "3001", 4),
        response={"quantity": 1},
    ),
    Case(
        "users_partlists_parts_update",
        "replace_user_part_list_part",
        (1, "3001", 4, QuantityRequest(quantity=2)),
        response={"quantity": 2},
        expected_data={"quantity": 2},
    ),
    Case(
        "users_partlists_parts_delete",
        "delete_user_part_list_part",
        (1, "3001", 4),
        status=204,
    ),
    Case("users_setlists_list", "list_user_set_lists", response=_PAGE),
    Case(
        "users_setlists_create",
        "create_user_set_list",
        (SetListRequest(name="s"),),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case(
        "users_setlists_read",
        "get_user_set_list",
        (1,),
        response={"id": 1, "name": "s"},
    ),
    Case(
        "users_setlists_update",
        "replace_user_set_list",
        (1, SetListRequest(name="s")),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case(
        "users_setlists_partial_update",
        "update_user_set_list",
        (1, SetListUpdateRequest(name="s")),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case("users_setlists_delete", "delete_user_set_list", (1,), status=204),
    Case("users_setlists_sets_list", "list_user_set_list_sets", (1,), response=_PAGE),
    Case(
        "users_setlists_sets_create",
        "add_user_set_list_sets",
        (1, SetQuantityRequest(set_num="1-1")),
        expected_data={"set_num": "1-1", "quantity": 1, "include_spares": False},
    ),
    Case(
        "users_setlists_sets_read",
        "get_user_set_list_set",
        (1, "1-1"),
        response={"set_num": "1-1"},
    ),
    Case(
        "users_setlists_sets_update",
        "replace_user_set_list_set",
        (1, "1-1", SetListSetUpdateRequest(quantity=2, include_spares=True)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2, "include_spares": True},
    ),
    Case(
        "users_setlists_sets_partial_update",
        "update_user_set_list_set",
        (1, "1-1", SetListSetUpdateRequest(quantity=2)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2},
    ),
    Case(
        "users_setlists_sets_delete",
        "delete_user_set_list_set",
        (1, "1-1"),
        status=204,
    ),
    Case("users_sets_list", "list_user_sets", response=_PAGE),
    Case(
        "users_sets_create",
        "add_user_sets",
        ((SetQuantityRequest(set_num="1-1"), SetQuantityRequest(set_num="2-1")),),
        response=[{"set_num": "1-1"}, {"set_num": "2-1"}],
        expected_json=[
            {"set_num": "1-1", "quantity": 1, "include_spares": False},
            {"set_num": "2-1", "quantity": 1, "include_spares": False},
        ],
    ),
    Case(
        "users_sets_sync_create",
        "sync_user_sets",
        (
            UserSetsSyncRequest(
                sets=(
                    SetQuantityRequest(set_num="8043-1"),
                    SetQuantityRequest(set_num="8110-1", quantity=2),
                )
            ),
        ),
        kwargs={"confirm_replace": True},
        response=[{"set_num": "8043-1"}, {"set_num": "8110-1"}],
        expected_json=[
            {"set_num": "8043-1", "quantity": 1, "include_spares": False},
            {"set_num": "8110-1", "quantity": 2, "include_spares": False},
        ],
    ),
    Case("users_sets_read", "get_user_set", ("1-1",), response={"set_num": "1-1"}),
    Case(
        "users_sets_update",
        "set_user_set_quantity",
        ("1-1", QuantityRequest(quantity=2)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2},
    ),
    Case("users_sets_delete", "delete_user_set", ("1-1",), status=204),
)


def _assert_conforms(
    operation: Operation, method: str, url: str, captured: dict[str, Any]
) -> None:
    assert method == operation.method
    assert "{" not in url
    base = "https://rebrickable.com/api/v3"
    assert url.startswith(base)
    pattern = re.escape(operation.path)
    for name in operation.path_parameters:
        pattern = pattern.replace(re.escape("{" + name + "}"), r"[^/]+")
    assert re.fullmatch(re.escape(base) + pattern, url), url

    params = captured["params"] or {}
    assert set(params) <= set(operation.query_parameters)

    data = captured["data"]
    json_body = captured["json"]
    required_form = set(operation.required_parameters) & set(operation.form_parameters)
    if data:
        assert operation.encoding == "application/x-www-form-urlencoded"
        assert set(data) <= set(operation.form_parameters)
        assert required_form <= set(data)
        assert all(
            isinstance(value, str | int | float | bool) for value in data.values()
        )
        assert json_body is None
    if json_body is not None:
        assert operation.method in {"POST", "PUT", "PATCH"}
        assert data is None
        for item in json_body:
            assert set(item) <= set(operation.form_parameters)
            assert required_form <= set(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.operation_id)
async def test_operation_request_conforms_to_registry(case: Case) -> None:
    transport = FakeTransport(FakeResponse(case.response, status_code=case.status))
    api = client(transport)
    await getattr(api, case.method_name)(*case.args, **case.kwargs)
    assert len(transport.requests) == 1
    method, url, captured = transport.requests[0]
    _assert_conforms(OPERATIONS[case.operation_id], method, url, captured)
    if case.expected_data is not None:
        assert captured["data"] == case.expected_data
    if case.expected_json is not _UNSET:
        assert captured["json"] == case.expected_json


def test_cases_cover_every_operation() -> None:
    assert {case.operation_id for case in CASES} == set(OPERATIONS)
