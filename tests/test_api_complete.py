# ruff: noqa: ANN003, ANN202, ARG001, ARG002, ARG005, PT018
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx2
import pytest

from rebrickable.api.client import RebrickableClient
from rebrickable.api.generated_requests import UsersTokenCreateParameters
from rebrickable.api.models import (
    ApiPage,
    ApiRecord,
    CreateUserTokenRequest,
    LostPartRequest,
    PartListPartRequest,
    PartListRequest,
    PartListUpdateRequest,
    QuantityRequest,
    SetListRequest,
    SetListUpdateRequest,
    SetQuantityRequest,
    UserSetsSyncRequest,
)
from rebrickable.config import Config
from rebrickable.errors import (
    ApiDecodeError,
    ApiError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiServerError,
    ApiThrottledError,
    PaginationCycleError,
    UserTokenRequiredError,
)

from .test_api import FakeResponse, FakeTransport, client


@pytest.mark.asyncio
async def test_every_public_operation_wrapper_and_iterator(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_model(self, operation_id, model, **kwargs):
        calls.append((operation_id, kwargs))
        return ApiRecord()

    async def fake_page(self, operation_id, item, **kwargs):
        calls.append((operation_id, kwargs))
        return ApiPage[Any](count=0, next=None, previous=None, results=())

    async def fake_iter(self, operation_id, item, **kwargs) -> AsyncIterator[Any]:
        calls.append((operation_id, kwargs))
        if False:  # pragma: no cover - makes this an async generator
            yield ApiRecord()

    monkeypatch.setattr(RebrickableClient, "_model", fake_model)
    monkeypatch.setattr(RebrickableClient, "_page", fake_page)
    monkeypatch.setattr(RebrickableClient, "_iter", fake_iter)
    api = RebrickableClient(
        api_key="key",
        user_token="token",
        config=Config(request_interval=0),
        transport=FakeTransport(),
    )

    reads: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
        ("list_colors", (), {"ordering": "id"}),
        ("get_color", (4,), {}),
        ("get_element", ("300121",), {}),
        ("list_minifigs", (), {}),
        ("get_minifig", ("fig-1",), {}),
        ("list_minifig_parts", ("fig-1",), {}),
        ("list_minifig_sets", ("fig-1",), {}),
        ("list_part_categories", (), {}),
        ("get_part_category", (1,), {}),
        ("list_parts", (), {"inc_part_details": True}),
        ("get_part", ("3001",), {}),
        ("list_part_colors", ("3001",), {}),
        ("get_part_color", ("3001", 4), {}),
        ("list_part_color_sets", ("3001", 4), {}),
        ("list_sets", (), {}),
        ("get_set", ("1-1",), {}),
        ("list_set_alternates", ("1-1",), {}),
        ("list_set_minifigs", ("1-1",), {}),
        ("list_set_parts", ("1-1",), {"inc_color_details": True}),
        ("list_set_sets", ("1-1",), {}),
        ("list_themes", (), {}),
        ("get_theme", (1,), {}),
        ("get_openapi_spec", (), {}),
        (
            "create_user_token",
            (CreateUserTokenRequest(username="u", password="p"),),
            {},
        ),
        ("list_badges", (), {}),
        ("get_badge", (1,), {}),
        ("list_user_all_parts", (), {}),
        ("get_user_build_requirements", ("1-1",), {}),
        ("list_user_lost_parts", (), {}),
        ("add_user_lost_parts", (LostPartRequest(inv_part_id=1),), {}),
        ("delete_user_lost_part", (1,), {}),
        ("list_user_minifigs", (), {}),
        ("list_user_parts", (), {}),
        ("get_user_profile", (), {}),
        ("list_user_part_lists", (), {}),
        ("create_user_part_list", (PartListRequest(name="p"),), {}),
        ("get_user_part_list", (1,), {}),
        ("replace_user_part_list", (1, PartListRequest(name="p")), {}),
        ("update_user_part_list", (1, PartListUpdateRequest(name="p")), {}),
        ("delete_user_part_list", (1,), {}),
        ("list_user_part_list_parts", (1,), {}),
        (
            "add_user_part_list_parts",
            (1, PartListPartRequest(part_num="3001", color_id=4, quantity=1)),
            {},
        ),
        ("get_user_part_list_part", (1, "3001", 4), {}),
        (
            "replace_user_part_list_part",
            (1, "3001", 4, QuantityRequest(quantity=2)),
            {},
        ),
        ("delete_user_part_list_part", (1, "3001", 4), {}),
        ("list_user_set_lists", (), {}),
        ("create_user_set_list", (SetListRequest(name="s"),), {}),
        ("get_user_set_list", (1,), {}),
        ("replace_user_set_list", (1, SetListRequest(name="s")), {}),
        ("update_user_set_list", (1, SetListUpdateRequest(name="s")), {}),
        ("delete_user_set_list", (1,), {}),
        ("list_user_set_list_sets", (1,), {}),
        ("add_user_set_list_sets", (1, SetQuantityRequest(set_num="1-1")), {}),
        ("get_user_set_list_set", (1, "1-1"), {}),
        ("replace_user_set_list_set", (1, "1-1", SetQuantityRequest(quantity=2)), {}),
        ("update_user_set_list_set", (1, "1-1", SetQuantityRequest(quantity=2)), {}),
        ("delete_user_set_list_set", (1, "1-1"), {}),
        ("list_user_sets", (), {}),
        ("add_user_sets", (SetQuantityRequest(set_num="1-1"),), {}),
        (
            "sync_user_sets",
            (UserSetsSyncRequest(sets=(SetQuantityRequest(set_num="1-1"),)),),
            {"confirm_replace": True},
        ),
        ("get_user_set", ("1-1",), {}),
        ("set_user_set_quantity", ("1-1", SetQuantityRequest(quantity=0)), {}),
        ("delete_user_set", ("1-1",), {}),
    ]
    for name, args, kwargs in reads:
        await getattr(api, name)(*args, **kwargs)
    with pytest.raises(ValueError, match="confirm_replace"):
        await api.sync_user_sets(UserSetsSyncRequest(sets=()))

    # Exercise sequence branches for the three batch wrappers.
    await api.add_user_part_list_parts(
        1, (PartListPartRequest(part_num="3002", color_id=1, quantity=2),)
    )
    await api.add_user_set_list_sets(1, (SetQuantityRequest(set_num="2-1"),))
    await api.add_user_sets((SetQuantityRequest(set_num="2-1"),))

    iterator_args = {
        "iter_minifig_parts": ("fig-1",),
        "iter_minifig_sets": ("fig-1",),
        "iter_part_colors": ("3001",),
        "iter_part_color_sets": ("3001", 4),
        "iter_set_alternates": ("1-1",),
        "iter_set_minifigs": ("1-1",),
        "iter_set_parts": ("1-1",),
        "iter_set_sets": ("1-1",),
        "iter_user_part_list_parts": (1,),
        "iter_user_set_list_sets": (1,),
    }
    iterator_names = [
        name
        for name in dir(RebrickableClient)
        if name.startswith("iter_") and callable(getattr(RebrickableClient, name))
    ]
    for name in iterator_names:
        iterator = getattr(api, name)(*iterator_args.get(name, ()))
        await iterator.aclose()

    assert {operation_id for operation_id, _ in calls} == set(api.operation_ids)


class SequenceTransport:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    async def request(self, method, url, *, headers, params=None, data=None):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.asyncio
async def test_transport_retries_errors_validation_and_close(monkeypatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("rebrickable.api.client.asyncio.sleep", no_sleep)
    monkeypatch.setattr("rebrickable.api.client.random.random", lambda: 0.0)
    transport = SequenceTransport(
        httpx2.ConnectError("down"),
        FakeResponse({}, status_code=503),
        FakeResponse({"part_num": "3001", "name": "Brick"}),
    )
    api = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=3),
        transport=transport,
    )
    assert (await api.get_part("3001")).part_num == "3001"
    assert transport.calls == 3
    with pytest.raises(TypeError, match="unsupported"):
        await api.get_part("3001", unsupported=True)
    with pytest.raises(TypeError, match="missing path"):
        await api._send("lego_parts_read")
    await api.close()
    await api.close()
    with pytest.raises(RuntimeError, match="closed"):
        await api.get_part("3001")

    for status, error in (
        (403, ApiForbiddenError),
        (404, ApiNotFoundError),
        (500, ApiServerError),
        (418, ApiError),
    ):
        failing = client(
            FakeTransport(FakeResponse({"detail": "no"}, status_code=status))
        )
        with pytest.raises(error):
            await failing.get_part("3001")

    throttled = RebrickableClient(
        api_key="secret-key",
        user_token="secret-token",
        config=Config(request_interval=0, max_retries=0),
        transport=FakeTransport(
            FakeResponse(
                {"detail": "retry after 2 seconds secret-key secret-token"},
                status_code=429,
                headers={"x-request-id": "request-1"},
            )
        ),
    )
    with pytest.raises(ApiThrottledError) as captured:
        await throttled.get_part("3001")
    assert captured.value.retry_after == 2
    assert "secret" not in str(captured.value)


def test_retry_after_decode_and_user_token_edges() -> None:
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    response = FakeResponse({}, headers={"retry-after": future})
    delay = RebrickableClient._retry_after(response, "")
    assert delay is not None and 0 <= delay <= 31
    assert (
        RebrickableClient._retry_after(
            FakeResponse({}, headers={"retry-after": "nonsense"}), "try later"
        )
        is None
    )
    assert (
        RebrickableClient._detail(
            type(
                "Raw",
                (),
                {
                    "text": "raw failure",
                    "json": lambda self: (_ for _ in ()).throw(ValueError()),
                },
            )()
        )
        == "raw failure"
    )
    api = RebrickableClient(api_key="key", transport=FakeTransport())
    with pytest.raises(UserTokenRequiredError):
        api._token(None)
    with pytest.raises(ValueError, match="api_key"):
        RebrickableClient(api_key="")


@pytest.mark.asyncio
async def test_decode_failure_204_and_pagination_cycle() -> None:
    invalid = client(FakeTransport(FakeResponse({"name": "missing id"})))
    with pytest.raises(ApiDecodeError):
        await invalid.get_color(4)

    deleted = client(FakeTransport(FakeResponse({}, status_code=204)))
    assert (await deleted.delete_user_set("1-1")).extra == {}

    looping = client(
        FakeTransport(
            FakeResponse(
                {
                    "count": 0,
                    "next": "https://rebrickable.com/api/v3/lego/colors/?page=2",
                    "previous": None,
                    "results": [],
                }
            ),
            FakeResponse(
                {
                    "count": 0,
                    "next": "https://rebrickable.com/api/v3/lego/colors/?page=2",
                    "previous": None,
                    "results": [],
                }
            ),
        )
    )
    with pytest.raises(PaginationCycleError):
        async for _ in looping.iter_colors():
            pass


@pytest.mark.asyncio
async def test_explicit_mutation_retry_policy_and_generated_secret_repr(
    monkeypatch,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("rebrickable.api.client.asyncio.sleep", no_sleep)
    transport = FakeTransport(
        FakeResponse({}, status_code=503),
        FakeResponse({"id": 1, "name": "List"}),
    )
    api = RebrickableClient(
        api_key="key",
        user_token="token",
        config=Config(request_interval=0, max_retries=1),
        transport=transport,
        mutation_retry_policy=lambda operation_id: (
            operation_id == "users_partlists_create"
        ),
    )
    result = await api.create_user_part_list(PartListRequest(name="List"))
    assert result.id == 1
    generated = UsersTokenCreateParameters(username="user", password="secret")
    assert "secret" not in repr(generated)
