"""Drive every registry operation through the real `_send`.

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


@dataclass(frozen=True, kw_only=True)
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
    Case(operation_id="lego_colors_list", method_name="list_colors", response=_PAGE),
    Case(
        operation_id="lego_colors_read",
        method_name="get_color",
        args=(4,),
        response={"id": 4, "name": "Red"},
    ),
    Case(
        operation_id="lego_elements_read",
        method_name="get_element",
        args=("300121",),
        response={"element_id": "300121"},
    ),
    Case(
        operation_id="lego_minifigs_list", method_name="list_minifigs", response=_PAGE
    ),
    Case(
        operation_id="lego_minifigs_read",
        method_name="get_minifig",
        args=("fig-1",),
        response={"set_num": "fig-1", "name": "Fig"},
    ),
    Case(
        operation_id="lego_minifigs_parts_list",
        method_name="list_minifig_parts",
        args=("fig-1",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_minifigs_sets_list",
        method_name="list_minifig_sets",
        args=("fig-1",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_part_categories_list",
        method_name="list_part_categories",
        response=_PAGE,
    ),
    Case(
        operation_id="lego_part_categories_read",
        method_name="get_part_category",
        args=(1,),
        response={"id": 1, "name": "Bricks"},
    ),
    Case(
        operation_id="lego_parts_list",
        method_name="list_parts",
        kwargs={"inc_part_details": True},
        response=_PAGE,
    ),
    Case(
        operation_id="lego_parts_read",
        method_name="get_part",
        args=("3001",),
        response={"part_num": "3001", "name": "Brick"},
    ),
    Case(
        operation_id="lego_parts_colors_list",
        method_name="list_part_colors",
        args=("3001",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_parts_colors_read",
        method_name="get_part_color",
        args=("3001", 4),
    ),
    Case(
        operation_id="lego_parts_colors_sets_list",
        method_name="list_part_color_sets",
        args=("3001", 4),
        response=_PAGE,
    ),
    Case(operation_id="lego_sets_list", method_name="list_sets", response=_PAGE),
    Case(
        operation_id="lego_sets_read",
        method_name="get_set",
        args=("1-1",),
        response={"set_num": "1-1", "name": "S"},
    ),
    Case(
        operation_id="lego_sets_alternates_list",
        method_name="list_set_alternates",
        args=("1-1",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_sets_minifigs_list",
        method_name="list_set_minifigs",
        args=("1-1",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_sets_parts_list",
        method_name="list_set_parts",
        args=("1-1",),
        response=_PAGE,
    ),
    Case(
        operation_id="lego_sets_sets_list",
        method_name="list_set_sets",
        args=("1-1",),
        response=_PAGE,
    ),
    Case(operation_id="lego_themes_list", method_name="list_themes", response=_PAGE),
    Case(
        operation_id="lego_themes_read",
        method_name="get_theme",
        args=(1,),
        response={"id": 1, "name": "Space"},
    ),
    Case(operation_id="swagger_list", method_name="get_openapi_spec"),
    Case(
        operation_id="users__token_create",
        method_name="create_user_token",
        args=(CreateUserTokenRequest(username="u", password="p"),),
        response={"user_token": "token-1"},
        expected_data={"username": "u", "password": "p"},
    ),
    Case(operation_id="users_badges_list", method_name="list_badges", response=_PAGE),
    Case(
        operation_id="users_badges_read",
        method_name="get_badge",
        args=(1,),
        response={"id": 1, "name": "Badge"},
    ),
    Case(
        operation_id="users_allparts_list",
        method_name="list_user_all_parts",
        response=_PAGE,
    ),
    Case(
        operation_id="users_build_read",
        method_name="get_user_build_requirements",
        args=("1-1",),
    ),
    Case(
        operation_id="users_lost_parts_list",
        method_name="list_user_lost_parts",
        response=_PAGE,
    ),
    Case(
        operation_id="users_lost_parts_create",
        method_name="add_user_lost_parts",
        args=(LostPartRequest(inv_part_id=1),),
        response={"id": 1},
        expected_data={"inv_part_id": 1},
    ),
    Case(
        operation_id="users_lost_parts_delete",
        method_name="delete_user_lost_part",
        args=(1,),
        status=204,
    ),
    Case(
        operation_id="users_minifigs_list",
        method_name="list_user_minifigs",
        response=_PAGE,
    ),
    Case(
        operation_id="users_parts_list", method_name="list_user_parts", response=_PAGE
    ),
    Case(
        operation_id="users_profile_read",
        method_name="get_user_profile",
        response={"username": "u"},
    ),
    Case(
        operation_id="users_partlists_list",
        method_name="list_user_part_lists",
        response=_PAGE,
    ),
    Case(
        operation_id="users_partlists_create",
        method_name="create_user_part_list",
        args=(PartListRequest(name="p"),),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case(
        operation_id="users_partlists_read",
        method_name="get_user_part_list",
        args=(1,),
        response={"id": 1, "name": "p"},
    ),
    Case(
        operation_id="users_partlists_update",
        method_name="replace_user_part_list",
        args=(1, PartListRequest(name="p")),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case(
        operation_id="users_partlists_partial_update",
        method_name="update_user_part_list",
        args=(1, PartListUpdateRequest(name="p")),
        response={"id": 1, "name": "p"},
        expected_data={"name": "p"},
    ),
    Case(
        operation_id="users_partlists_delete",
        method_name="delete_user_part_list",
        args=(1,),
        status=204,
    ),
    Case(
        operation_id="users_partlists_parts_list",
        method_name="list_user_part_list_parts",
        args=(1,),
        response=_PAGE,
    ),
    Case(
        operation_id="users_partlists_parts_create",
        method_name="add_user_part_list_parts",
        args=(1, PartListPartRequest(part_num="3001", color_id=4, quantity=1)),
        expected_data={"part_num": "3001", "color_id": 4, "quantity": 1},
    ),
    Case(
        operation_id="users_partlists_parts_read",
        method_name="get_user_part_list_part",
        args=(1, "3001", 4),
        response={"quantity": 1},
    ),
    Case(
        operation_id="users_partlists_parts_update",
        method_name="replace_user_part_list_part",
        args=(1, "3001", 4, QuantityRequest(quantity=2)),
        response={"quantity": 2},
        expected_data={"quantity": 2},
    ),
    Case(
        operation_id="users_partlists_parts_delete",
        method_name="delete_user_part_list_part",
        args=(1, "3001", 4),
        status=204,
    ),
    Case(
        operation_id="users_setlists_list",
        method_name="list_user_set_lists",
        response=_PAGE,
    ),
    Case(
        operation_id="users_setlists_create",
        method_name="create_user_set_list",
        args=(SetListRequest(name="s"),),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case(
        operation_id="users_setlists_read",
        method_name="get_user_set_list",
        args=(1,),
        response={"id": 1, "name": "s"},
    ),
    Case(
        operation_id="users_setlists_update",
        method_name="replace_user_set_list",
        args=(1, SetListRequest(name="s")),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case(
        operation_id="users_setlists_partial_update",
        method_name="update_user_set_list",
        args=(1, SetListUpdateRequest(name="s")),
        response={"id": 1, "name": "s"},
        expected_data={"name": "s"},
    ),
    Case(
        operation_id="users_setlists_delete",
        method_name="delete_user_set_list",
        args=(1,),
        status=204,
    ),
    Case(
        operation_id="users_setlists_sets_list",
        method_name="list_user_set_list_sets",
        args=(1,),
        response=_PAGE,
    ),
    Case(
        operation_id="users_setlists_sets_create",
        method_name="add_user_set_list_sets",
        args=(1, SetQuantityRequest(set_num="1-1")),
        expected_data={"set_num": "1-1", "quantity": 1, "include_spares": False},
    ),
    Case(
        operation_id="users_setlists_sets_read",
        method_name="get_user_set_list_set",
        args=(1, "1-1"),
        response={"set_num": "1-1"},
    ),
    Case(
        operation_id="users_setlists_sets_update",
        method_name="replace_user_set_list_set",
        args=(1, "1-1", SetListSetUpdateRequest(quantity=2, include_spares=True)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2, "include_spares": True},
    ),
    Case(
        operation_id="users_setlists_sets_partial_update",
        method_name="update_user_set_list_set",
        args=(1, "1-1", SetListSetUpdateRequest(quantity=2)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2},
    ),
    Case(
        operation_id="users_setlists_sets_delete",
        method_name="delete_user_set_list_set",
        args=(1, "1-1"),
        status=204,
    ),
    Case(operation_id="users_sets_list", method_name="list_user_sets", response=_PAGE),
    Case(
        operation_id="users_sets_create",
        method_name="add_user_sets",
        args=((SetQuantityRequest(set_num="1-1"), SetQuantityRequest(set_num="2-1")),),
        response=[{"set_num": "1-1"}, {"set_num": "2-1"}],
        expected_json=[
            {"set_num": "1-1", "quantity": 1, "include_spares": False},
            {"set_num": "2-1", "quantity": 1, "include_spares": False},
        ],
    ),
    Case(
        operation_id="users_sets_sync_create",
        method_name="sync_user_sets",
        args=(
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
    Case(
        operation_id="users_sets_read",
        method_name="get_user_set",
        args=("1-1",),
        response={"set_num": "1-1"},
    ),
    Case(
        operation_id="users_sets_update",
        method_name="set_user_set_quantity",
        args=("1-1", QuantityRequest(quantity=2)),
        response={"set_num": "1-1"},
        expected_data={"quantity": 2},
    ),
    Case(
        operation_id="users_sets_delete",
        method_name="delete_user_set",
        args=("1-1",),
        status=204,
    ),
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
    transport = FakeTransport(
        FakeResponse(payload=case.response, status_code=case.status)
    )
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
