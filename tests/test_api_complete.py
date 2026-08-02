# ruff: noqa: ANN003, ANN202, ARG001, ARG002, ARG005
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any, ClassVar

import httpx2
import pytest

from rebrickable.api.client import RebrickableClient
from rebrickable.api.generated_requests import UsersTokenCreateParameters
from rebrickable.api.models import (
    ApiPage,
    ApiRecord,
    CreateUserTokenRequest,
    LostPartRequest,
    MutationResult,
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
from rebrickable.config import Config
from rebrickable.errors import (
    ApiDecodeError,
    ApiError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiServerError,
    ApiThrottledError,
    BatchMutationError,
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

    async def fake_mutation(self, operation_id, **kwargs):
        calls.append((operation_id, kwargs))
        return MutationResult()

    monkeypatch.setattr(RebrickableClient, "_model", fake_model)
    monkeypatch.setattr(RebrickableClient, "_page", fake_page)
    monkeypatch.setattr(RebrickableClient, "_iter", fake_iter)
    monkeypatch.setattr(RebrickableClient, "_mutation", fake_mutation)
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
        (
            "replace_user_set_list_set",
            (1, "1-1", SetListSetUpdateRequest(quantity=2)),
            {},
        ),
        (
            "update_user_set_list_set",
            (1, "1-1", SetListSetUpdateRequest(quantity=2)),
            {},
        ),
        ("delete_user_set_list_set", (1, "1-1"), {}),
        ("list_user_sets", (), {}),
        ("add_user_sets", (SetQuantityRequest(set_num="1-1"),), {}),
        (
            "sync_user_sets",
            (UserSetsSyncRequest(sets=(SetQuantityRequest(set_num="1-1"),)),),
            {"confirm_replace": True},
        ),
        ("get_user_set", ("1-1",), {}),
        ("set_user_set_quantity", ("1-1", QuantityRequest(quantity=0)), {}),
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

    async def request(self, method, url, *, headers, params=None, data=None, json=None):
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


@pytest.mark.asyncio
async def test_retry_after_cap_raises_instead_of_sleeping() -> None:
    transport = FakeTransport(
        FakeResponse({}, status_code=429, headers={"retry-after": "99999"})
    )
    api = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=3, max_retry_after=10),
        transport=transport,
    )
    with pytest.raises(ApiThrottledError) as captured:
        await api.get_part("3001")
    assert captured.value.retry_after == 99999
    assert len(transport.requests) == 1
    assert api._throttle_until == 0.0


@pytest.mark.asyncio
async def test_redirect_and_non_json_success_are_structured_errors() -> None:
    redirecting = client(
        FakeTransport(
            FakeResponse(
                {}, status_code=302, headers={"location": "https://rebrickable.com/x"}
            )
        )
    )
    with pytest.raises(ApiError) as captured:
        await redirecting.get_part("3001")
    assert captured.value.status == 302

    class HtmlResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        text = "<html>proxy interstitial</html>"

        def json(self) -> Any:
            raise ValueError("not json")

    class HtmlTransport:
        async def request(
            self, method, url, *, headers, params=None, data=None, json=None
        ):
            return HtmlResponse()

    broken = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=0),
        transport=HtmlTransport(),
    )
    with pytest.raises(ApiDecodeError, match="not valid JSON"):
        await broken.get_part("3001")


@pytest.mark.asyncio
async def test_sync_user_sets_posts_json_array() -> None:
    transport = FakeTransport(
        FakeResponse(
            [
                {"set_num": "8043-1", "quantity": 1},
                {"set_num": "8110-1", "quantity": 3},
            ]
        )
    )
    api = client(transport)
    result = await api.sync_user_sets(
        UserSetsSyncRequest(
            sets=(
                SetQuantityRequest(set_num="8043-1"),
                SetQuantityRequest(set_num="8110-1", quantity=3),
            )
        ),
        confirm_replace=True,
    )
    assert len(result.accepted) == 2
    method, url, captured = transport.requests[-1]
    assert method == "POST"
    assert url.endswith("/users/user-secret/sets/sync/")
    assert captured["json"] == [
        {"set_num": "8043-1", "quantity": 1, "include_spares": False},
        {"set_num": "8110-1", "quantity": 3, "include_spares": False},
    ]
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_sync_user_sets_empty_payload_sends_empty_list() -> None:
    transport = FakeTransport(FakeResponse([]))
    api = client(transport)
    result = await api.sync_user_sets(
        UserSetsSyncRequest(sets=()), confirm_replace=True
    )
    assert result.accepted == ()
    assert transport.requests[-1][2]["json"] == []


def test_sync_request_model_requires_set_num() -> None:
    with pytest.raises(ValueError, match="set_num"):
        UserSetsSyncRequest(sets=(SetQuantityRequest(quantity=1),))


@pytest.mark.asyncio
async def test_add_user_sets_sequence_is_single_json_post() -> None:
    transport = FakeTransport(FakeResponse({"set_num": "1-1"}))
    api = client(transport)
    scalar = await api.add_user_sets(SetQuantityRequest(set_num="1-1"))
    assert len(scalar.accepted) == 1
    assert transport.requests[-1][2]["data"] == {
        "set_num": "1-1",
        "quantity": 1,
        "include_spares": False,
    }
    assert transport.requests[-1][2]["json"] is None

    batching = FakeTransport(FakeResponse([{"set_num": "1-1"}, {"set_num": "2-1"}]))
    api = client(batching)
    result = await api.add_user_sets(
        (SetQuantityRequest(set_num="1-1"), SetQuantityRequest(set_num="2-1"))
    )
    assert len(result.accepted) == 2
    assert len(batching.requests) == 1
    assert batching.requests[-1][2]["json"] == [
        {"set_num": "1-1", "quantity": 1, "include_spares": False},
        {"set_num": "2-1", "quantity": 1, "include_spares": False},
    ]


@pytest.mark.asyncio
async def test_batch_mutation_error_carries_partial_progress() -> None:
    transport = FakeTransport(
        FakeResponse({"part_num": "3001"}),
        FakeResponse({"detail": "no"}, status_code=400),
    )
    api = client(transport)
    with pytest.raises(BatchMutationError) as captured:
        await api.add_user_part_list_parts(
            1,
            (
                PartListPartRequest(part_num="3001", color_id=4, quantity=1),
                PartListPartRequest(part_num="3002", color_id=4, quantity=1),
            ),
        )
    assert captured.value.failed_index == 1
    assert len(captured.value.accepted) == 1
    assert isinstance(captured.value.__cause__, ApiError)


@pytest.mark.asyncio
async def test_send_json_body_validation_and_wire_shape() -> None:
    transport = FakeTransport(FakeResponse([{"set_num": "1-1"}]))
    api = client(transport)
    path = {"user_token": "user-secret"}
    with pytest.raises(TypeError, match="mutually exclusive"):
        await api._send(
            "users_sets_sync_create",
            path_values=path,
            form={"set_num": "1-1"},
            json_body=[],
        )
    with pytest.raises(TypeError, match="missing required parameters: set_num"):
        await api._send("users_sets_create", path_values=path, form={"quantity": 1})
    with pytest.raises(TypeError, match="does not accept a JSON body"):
        await api._send("lego_parts_list", json_body=[])
    with pytest.raises(TypeError, match="unsupported parameters: bogus"):
        await api._send(
            "users_sets_sync_create", path_values=path, json_body=[{"bogus": 1}]
        )
    with pytest.raises(TypeError, match="missing required parameters: set_num"):
        await api._send(
            "users_sets_sync_create", path_values=path, json_body=[{"quantity": 1}]
        )
    response = await api._send(
        "users_sets_sync_create",
        path_values=path,
        json_body=[{"set_num": "1-1", "quantity": 1}],
    )
    assert response.status_code == 200
    method, url, captured = transport.requests[-1]
    assert method == "POST"
    assert url.endswith("/users/user-secret/sets/sync/")
    assert captured["json"] == [{"set_num": "1-1", "quantity": 1}]
    assert captured["data"] is None


@pytest.mark.asyncio
async def test_pagination_cycle_and_per_call_token_redaction() -> None:
    loop_page = {
        "count": 0,
        "next": "https://rebrickable.com/api/v3/users/user-secret/sets/?page=2",
        "previous": None,
        "results": [],
    }
    looping = client(FakeTransport(FakeResponse(loop_page), FakeResponse(loop_page)))
    with pytest.raises(PaginationCycleError) as cycle:
        async for _ in looping.iter_user_sets():
            pass
    assert "user-secret" not in str(cycle.value)
    assert "<redacted>" in str(cycle.value)

    tokenless = RebrickableClient(
        api_key="key",
        config=Config(request_interval=0, max_retries=0),
        transport=FakeTransport(
            FakeResponse({"detail": "unknown token percall-secret"}, status_code=404)
        ),
    )
    with pytest.raises(ApiError) as captured:
        await tokenless.list_user_sets(user_token="percall-secret")
    assert "percall-secret" not in str(captured.value)
    assert "<redacted>" in str(captured.value)


def test_retry_after_decode_and_user_token_edges() -> None:
    fixed = datetime(2026, 8, 1, tzinfo=UTC)
    header = format_datetime(fixed + timedelta(seconds=30))
    response = FakeResponse({}, headers={"retry-after": header})
    delay = RebrickableClient._retry_after(response, "", now=fixed)
    assert delay == 30.0
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
