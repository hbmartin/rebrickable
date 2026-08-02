"""Complete asynchronous Rebrickable API v3 client."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar, Unpack, cast
from urllib.parse import quote

import httpx2
from pydantic import BaseModel

from rebrickable.api import query_types as qt
from rebrickable.api.decoding import decode_model
from rebrickable.api.models import (
    ApiAlternateBuild,
    ApiBadge,
    ApiBuildResult,
    ApiColor,
    ApiElement,
    ApiInventoryMinifig,
    ApiInventoryPart,
    ApiInventorySet,
    ApiLostPart,
    ApiMinifig,
    ApiPage,
    ApiPart,
    ApiPartCategory,
    ApiPartColor,
    ApiPartList,
    ApiPartListPart,
    ApiProfile,
    ApiRecord,
    ApiSet,
    ApiSetList,
    ApiSetListSet,
    ApiTheme,
    ApiUserMinifig,
    ApiUserPart,
    ApiUserSet,
    ApiUserToken,
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
from rebrickable.api.operation_registry import OPERATIONS
from rebrickable.api.pagination import validate_next_url
from rebrickable.api.transport import AsyncTransport, HttpxTransport, ResponseLike
from rebrickable.config import Config
from rebrickable.errors import (
    ApiAuthenticationError,
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

T = TypeVar("T", bound=BaseModel)
MutationRetryPolicy = Callable[[str], bool]

_TRANSIENT = {500, 502, 503, 504}
_THROTTLE_SECONDS = re.compile(
    r"(?:in|after)\s+(\d+(?:\.\d+)?)\s+seconds?", re.IGNORECASE
)


class RebrickableClient:
    """Typed API v3 client with header-only authentication and safe pacing."""

    def __init__(
        self,
        *,
        api_key: str,
        user_token: str | None = None,
        config: Config | None = None,
        transport: AsyncTransport | None = None,
        mutation_retry_policy: MutationRetryPolicy | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._user_token = user_token
        self._secrets = {value for value in (api_key, user_token) if value}
        self._config = Config() if config is None else config
        timeout = httpx2.Timeout(
            connect=self._config.connect_timeout,
            read=self._config.read_timeout,
            write=self._config.write_timeout,
            pool=self._config.pool_timeout,
        )
        self._transport: AsyncTransport = transport or HttpxTransport(timeout=timeout)
        self._owns_transport = transport is None
        self._mutation_retry_policy = mutation_retry_policy
        self._pace_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._throttle_until = 0.0
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=<redacted>, user_token={'<set>' if self._user_token else None})"

    async def __aenter__(self) -> RebrickableClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport and isinstance(self._transport, HttpxTransport):
            await self._transport.close()

    def _token(self, token: str | None) -> str:
        resolved = token or self._user_token
        if not resolved:
            raise UserTokenRequiredError("user_token is required for this operation")
        self._secrets.add(resolved)
        return resolved

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            now = loop.time()
            ready = max(self._next_request_at, self._throttle_until)
            if ready > now:
                await asyncio.sleep(ready - now)
            self._next_request_at = loop.time() + self._config.request_interval

    @staticmethod
    def _detail(response: ResponseLike) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return response.text[:400] or "upstream request failed"
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            return payload["detail"][:400]
        return "upstream request failed"

    @staticmethod
    def _retry_after(
        response: ResponseLike, detail: str, *, now: datetime | None = None
    ) -> float | None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(header)
                    reference = now or datetime.now(UTC)
                    return max(0.0, (parsed - reference).total_seconds())
                except (TypeError, ValueError):
                    pass
        match = _THROTTLE_SECONDS.search(detail)
        return float(match.group(1)) if match else None

    def _error(
        self,
        response: ResponseLike,
        *,
        operation_id: str,
        path_template: str,
        retry_after: float | None = None,
    ) -> ApiError:
        detail = self._redact(self._detail(response))
        request_id = response.headers.get("x-request-id")
        values = (
            response.status_code,
            path_template,
            operation_id,
            detail,
            request_id,
            retry_after,
        )
        if response.status_code == 401:
            return ApiAuthenticationError(*values)
        if response.status_code == 403:
            return ApiForbiddenError(*values)
        if response.status_code == 404:
            return ApiNotFoundError(*values)
        if response.status_code == 429:
            return ApiThrottledError(*values)
        if response.status_code >= 500:
            return ApiServerError(*values)
        return ApiError(*values)

    async def _send(
        self,
        operation_id: str,
        *,
        path_values: Mapping[str, str | int] | None = None,
        query: Mapping[str, object] | None = None,
        form: Mapping[str, Any] | None = None,
        json_body: list[dict[str, Any]] | None = None,
        absolute_url: str | None = None,
        retry_mutation: bool = False,
    ) -> ResponseLike:
        if self._closed:
            raise RuntimeError("client is closed")
        operation = OPERATIONS[operation_id]
        path_values = dict(path_values or {})
        path = operation.path
        for name in operation.path_parameters:
            if name not in path_values:
                raise TypeError(f"missing path parameter: {name}")
            path = path.replace(
                "{" + name + "}", quote(str(path_values[name]), safe="")
            )
        query_data = {
            key: value for key, value in (query or {}).items() if value is not None
        }
        form_data = {
            key: value for key, value in (form or {}).items() if value is not None
        }
        unknown_query = set(query_data) - set(operation.query_parameters)
        unknown_form = set(form_data) - set(operation.form_parameters)
        if unknown_query or unknown_form:
            unknown = sorted(unknown_query | unknown_form)
            raise TypeError("unsupported parameters: " + ", ".join(unknown))
        required_form = set(operation.required_parameters) & set(
            operation.form_parameters
        )
        if form_data:
            if json_body is not None:
                raise TypeError("form and json_body are mutually exclusive")
            missing = required_form - set(form_data)
            if missing:
                raise TypeError(
                    "missing required parameters: " + ", ".join(sorted(missing))
                )
        if json_body is not None:
            if (
                operation.method not in {"POST", "PUT", "PATCH"}
                or not operation.form_parameters
            ):
                raise TypeError(f"{operation_id} does not accept a JSON body")
            for item in json_body:
                unknown_item = set(item) - set(operation.form_parameters)
                if unknown_item:
                    raise TypeError(
                        "unsupported parameters: " + ", ".join(sorted(unknown_item))
                    )
                missing = required_form - set(item)
                if missing:
                    raise TypeError(
                        "missing required parameters: " + ", ".join(sorted(missing))
                    )
        url = absolute_url or f"{self._config.base_url.rstrip('/')}{path}"
        idempotent = operation.method in {"GET", "DELETE"}
        retry_mutation = retry_mutation or bool(
            self._mutation_retry_policy and self._mutation_retry_policy(operation_id)
        )
        max_attempts = (
            self._config.max_retries + 1 if idempotent or retry_mutation else 1
        )
        for attempt in range(max_attempts):
            await self._pace()
            try:
                headers = {
                    "Authorization": f"key {self._api_key}",
                    "Accept": "application/json",
                }
                if json_body is None:
                    response = await self._transport.request(
                        operation.method,
                        url,
                        headers=headers,
                        params=query_data or None,
                        data=form_data or None,
                    )
                else:
                    response = await self._transport.request(
                        operation.method,
                        url,
                        headers=headers,
                        params=query_data or None,
                        data=form_data or None,
                        json=json_body,
                    )
            except (httpx2.TransportError, OSError) as exc:
                if attempt + 1 >= max_attempts:
                    raise ApiError(
                        None, operation.path, operation_id, "transport failure"
                    ) from exc
                await asyncio.sleep(min(8.0, 2**attempt + random.random()))
                continue
            if response.status_code == 429:
                detail = self._detail(response)
                delay = self._retry_after(response, detail) or min(
                    30.0, 2**attempt + random.random()
                )
                if delay > self._config.max_retry_after:
                    raise self._error(
                        response,
                        operation_id=operation_id,
                        path_template=operation.path,
                        retry_after=delay,
                    )
                self._throttle_until = max(
                    asyncio.get_running_loop().time() + delay, self._throttle_until
                )
                if attempt + 1 < max_attempts:
                    continue
                raise self._error(
                    response,
                    operation_id=operation_id,
                    path_template=operation.path,
                    retry_after=delay,
                )
            if response.status_code in _TRANSIENT and attempt + 1 < max_attempts:
                await asyncio.sleep(min(8.0, 2**attempt + random.random()))
                continue
            if response.status_code >= 300:
                raise self._error(
                    response, operation_id=operation_id, path_template=operation.path
                )
            return response
        raise AssertionError("request loop exhausted")  # pragma: no cover

    def _json(self, response: ResponseLike, *, operation_id: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            operation = OPERATIONS[operation_id]
            raise ApiDecodeError(
                operation_id=operation_id,
                path_template=operation.path,
                detail="successful response body was not valid JSON",
            ) from exc

    async def _model(
        self,
        operation_id: str,
        model: type[T],
        *,
        path: Mapping[str, str | int] | None = None,
        query: Mapping[str, object] | None = None,
        form: Mapping[str, Any] | None = None,
        retry_mutation: bool = False,
    ) -> T:
        response = await self._send(
            operation_id,
            path_values=path,
            query=query,
            form=form,
            retry_mutation=retry_mutation,
        )
        payload = (
            {}
            if response.status_code == 204 or not response.text
            else self._json(response, operation_id=operation_id)
        )
        operation = OPERATIONS[operation_id]
        return decode_model(
            model, payload, operation_id=operation_id, path_template=operation.path
        )

    async def _mutation(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str | int],
        json_body: list[dict[str, Any]],
    ) -> MutationResult:
        response = await self._send(operation_id, path_values=path, json_body=json_body)
        operation = OPERATIONS[operation_id]
        payload = (
            []
            if response.status_code == 204 or not response.text
            else self._json(response, operation_id=operation_id)
        )
        requested = tuple(ApiRecord.model_validate(item) for item in json_body)

        def records(value: object) -> tuple[ApiRecord, ...]:
            if value is None:
                return ()
            items = value if isinstance(value, list) else [value]
            return tuple(
                decode_model(
                    ApiRecord,
                    item if isinstance(item, Mapping) else {"detail": item},
                    operation_id=operation_id,
                    path_template=operation.path,
                )
                for item in items
            )

        if response.status_code == 204 or not response.text:
            return MutationResult(requested=requested, accepted=requested)
        if isinstance(payload, dict) and any(
            key in payload
            for key in (
                "accepted",
                "results",
                "created",
                "skipped",
                "rejected",
                "errors",
            )
        ):
            accepted = records(
                payload.get("accepted", payload.get("results", payload.get("created")))
            )
            skipped = records(payload.get("skipped"))
            unaccepted = records(payload.get("rejected", payload.get("errors")))
        else:
            accepted = records(payload)
            skipped = ()
            unaccepted = ()
        if not unaccepted and len(accepted) + len(skipped) < len(requested):
            unaccepted = requested[len(accepted) + len(skipped) :]
        return MutationResult(
            requested=requested,
            accepted=accepted,
            unaccepted=unaccepted,
            skipped=skipped,
        )

    async def _page(
        self,
        operation_id: str,
        item: type[T],
        *,
        path: Mapping[str, str | int] | None = None,
        query: Mapping[str, object] | None = None,
        absolute_url: str | None = None,
    ) -> ApiPage[T]:
        response = await self._send(
            operation_id, path_values=path, query=query, absolute_url=absolute_url
        )
        operation = OPERATIONS[operation_id]
        page_model = ApiPage[item]  # ty: ignore[invalid-type-form]
        return cast(
            "ApiPage[T]",
            decode_model(
                page_model,
                self._json(response, operation_id=operation_id),
                operation_id=operation_id,
                path_template=operation.path,
            ),
        )

    async def _iter(
        self,
        operation_id: str,
        item: type[T],
        *,
        path: Mapping[str, str | int] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> AsyncIterator[T]:
        options = dict(query or {})
        options.setdefault("page_size", 1_000)
        page = await self._page(operation_id, item, path=path, query=options)
        visited: set[str] = set()
        while True:
            yield_values = page.results
            for result in yield_values:
                yield result
            if page.next is None:
                return
            next_url = validate_next_url(str(page.next), self._config.base_url)
            if next_url in visited:
                raise PaginationCycleError(self._redact(next_url))
            visited.add(next_url)
            page = await self._page(
                operation_id, item, path=path, absolute_url=next_url
            )

    # Catalog and specification -------------------------------------------------
    async def list_colors(
        self, **query: Unpack[qt.LegoColorsListQuery]
    ) -> ApiPage[ApiColor]:
        return await self._page("lego_colors_list", ApiColor, query=query)

    def iter_colors(
        self, **query: Unpack[qt.LegoColorsListQuery]
    ) -> AsyncIterator[ApiColor]:
        return self._iter("lego_colors_list", ApiColor, query=query)

    async def get_color(
        self, color_id: int, **query: Unpack[qt.LegoColorsReadQuery]
    ) -> ApiColor:
        return await self._model(
            "lego_colors_read", ApiColor, path={"id": color_id}, query=query
        )

    async def get_element(self, element_id: str) -> ApiElement:
        return await self._model(
            "lego_elements_read", ApiElement, path={"element_id": element_id}
        )

    async def list_minifigs(
        self, **query: Unpack[qt.LegoMinifigsListQuery]
    ) -> ApiPage[ApiMinifig]:
        return await self._page("lego_minifigs_list", ApiMinifig, query=query)

    def iter_minifigs(
        self, **query: Unpack[qt.LegoMinifigsListQuery]
    ) -> AsyncIterator[ApiMinifig]:
        return self._iter("lego_minifigs_list", ApiMinifig, query=query)

    async def get_minifig(self, fig_num: str) -> ApiMinifig:
        return await self._model(
            "lego_minifigs_read", ApiMinifig, path={"set_num": fig_num}
        )

    async def list_minifig_parts(
        self, fig_num: str, **query: Unpack[qt.LegoMinifigsPartsListQuery]
    ) -> ApiPage[ApiInventoryPart]:
        return await self._page(
            "lego_minifigs_parts_list",
            ApiInventoryPart,
            path={"set_num": fig_num},
            query=query,
        )

    def iter_minifig_parts(
        self, fig_num: str, **query: Unpack[qt.LegoMinifigsPartsListQuery]
    ) -> AsyncIterator[ApiInventoryPart]:
        return self._iter(
            "lego_minifigs_parts_list",
            ApiInventoryPart,
            path={"set_num": fig_num},
            query=query,
        )

    async def list_minifig_sets(
        self, fig_num: str, **query: Unpack[qt.LegoMinifigsSetsListQuery]
    ) -> ApiPage[ApiSet]:
        return await self._page(
            "lego_minifigs_sets_list", ApiSet, path={"set_num": fig_num}, query=query
        )

    def iter_minifig_sets(
        self, fig_num: str, **query: Unpack[qt.LegoMinifigsSetsListQuery]
    ) -> AsyncIterator[ApiSet]:
        return self._iter(
            "lego_minifigs_sets_list", ApiSet, path={"set_num": fig_num}, query=query
        )

    async def list_part_categories(
        self, **query: Unpack[qt.LegoPartCategoriesListQuery]
    ) -> ApiPage[ApiPartCategory]:
        return await self._page(
            "lego_part_categories_list", ApiPartCategory, query=query
        )

    def iter_part_categories(
        self, **query: Unpack[qt.LegoPartCategoriesListQuery]
    ) -> AsyncIterator[ApiPartCategory]:
        return self._iter("lego_part_categories_list", ApiPartCategory, query=query)

    async def get_part_category(
        self, category_id: int, **query: Unpack[qt.LegoPartCategoriesReadQuery]
    ) -> ApiPartCategory:
        return await self._model(
            "lego_part_categories_read",
            ApiPartCategory,
            path={"id": category_id},
            query=query,
        )

    async def list_parts(
        self, **query: Unpack[qt.LegoPartsListQuery]
    ) -> ApiPage[ApiPart]:
        return await self._page("lego_parts_list", ApiPart, query=query)

    def iter_parts(
        self, **query: Unpack[qt.LegoPartsListQuery]
    ) -> AsyncIterator[ApiPart]:
        return self._iter("lego_parts_list", ApiPart, query=query)

    async def get_part(
        self, part_num: str, **query: Unpack[qt.LegoPartsReadQuery]
    ) -> ApiPart:
        return await self._model(
            "lego_parts_read", ApiPart, path={"part_num": part_num}, query=query
        )

    async def list_part_colors(
        self, part_num: str, **query: Unpack[qt.LegoPartsColorsListQuery]
    ) -> ApiPage[ApiPartColor]:
        return await self._page(
            "lego_parts_colors_list",
            ApiPartColor,
            path={"part_num": part_num},
            query=query,
        )

    def iter_part_colors(
        self, part_num: str, **query: Unpack[qt.LegoPartsColorsListQuery]
    ) -> AsyncIterator[ApiPartColor]:
        return self._iter(
            "lego_parts_colors_list",
            ApiPartColor,
            path={"part_num": part_num},
            query=query,
        )

    async def get_part_color(self, part_num: str, color_id: int) -> ApiPartColor:
        return await self._model(
            "lego_parts_colors_read",
            ApiPartColor,
            path={"part_num": part_num, "color_id": color_id},
        )

    async def list_part_color_sets(
        self,
        part_num: str,
        color_id: int,
        **query: Unpack[qt.LegoPartsColorsSetsListQuery],
    ) -> ApiPage[ApiSet]:
        return await self._page(
            "lego_parts_colors_sets_list",
            ApiSet,
            path={"part_num": part_num, "color_id": color_id},
            query=query,
        )

    def iter_part_color_sets(
        self,
        part_num: str,
        color_id: int,
        **query: Unpack[qt.LegoPartsColorsSetsListQuery],
    ) -> AsyncIterator[ApiSet]:
        return self._iter(
            "lego_parts_colors_sets_list",
            ApiSet,
            path={"part_num": part_num, "color_id": color_id},
            query=query,
        )

    async def list_sets(self, **query: Unpack[qt.LegoSetsListQuery]) -> ApiPage[ApiSet]:
        return await self._page("lego_sets_list", ApiSet, query=query)

    def iter_sets(self, **query: Unpack[qt.LegoSetsListQuery]) -> AsyncIterator[ApiSet]:
        return self._iter("lego_sets_list", ApiSet, query=query)

    async def get_set(self, set_num: str) -> ApiSet:
        return await self._model("lego_sets_read", ApiSet, path={"set_num": set_num})

    async def list_set_alternates(
        self, set_num: str, **query: Unpack[qt.LegoSetsAlternatesListQuery]
    ) -> ApiPage[ApiAlternateBuild]:
        return await self._page(
            "lego_sets_alternates_list",
            ApiAlternateBuild,
            path={"set_num": set_num},
            query=query,
        )

    def iter_set_alternates(
        self, set_num: str, **query: Unpack[qt.LegoSetsAlternatesListQuery]
    ) -> AsyncIterator[ApiAlternateBuild]:
        return self._iter(
            "lego_sets_alternates_list",
            ApiAlternateBuild,
            path={"set_num": set_num},
            query=query,
        )

    async def list_set_minifigs(
        self, set_num: str, **query: Unpack[qt.LegoSetsMinifigsListQuery]
    ) -> ApiPage[ApiInventoryMinifig]:
        return await self._page(
            "lego_sets_minifigs_list",
            ApiInventoryMinifig,
            path={"set_num": set_num},
            query=query,
        )

    def iter_set_minifigs(
        self, set_num: str, **query: Unpack[qt.LegoSetsMinifigsListQuery]
    ) -> AsyncIterator[ApiInventoryMinifig]:
        return self._iter(
            "lego_sets_minifigs_list",
            ApiInventoryMinifig,
            path={"set_num": set_num},
            query=query,
        )

    async def list_set_parts(
        self, set_num: str, **query: Unpack[qt.LegoSetsPartsListQuery]
    ) -> ApiPage[ApiInventoryPart]:
        return await self._page(
            "lego_sets_parts_list",
            ApiInventoryPart,
            path={"set_num": set_num},
            query=query,
        )

    def iter_set_parts(
        self, set_num: str, **query: Unpack[qt.LegoSetsPartsListQuery]
    ) -> AsyncIterator[ApiInventoryPart]:
        return self._iter(
            "lego_sets_parts_list",
            ApiInventoryPart,
            path={"set_num": set_num},
            query=query,
        )

    async def list_set_sets(
        self, set_num: str, **query: Unpack[qt.LegoSetsSetsListQuery]
    ) -> ApiPage[ApiInventorySet]:
        return await self._page(
            "lego_sets_sets_list",
            ApiInventorySet,
            path={"set_num": set_num},
            query=query,
        )

    def iter_set_sets(
        self, set_num: str, **query: Unpack[qt.LegoSetsSetsListQuery]
    ) -> AsyncIterator[ApiInventorySet]:
        return self._iter(
            "lego_sets_sets_list",
            ApiInventorySet,
            path={"set_num": set_num},
            query=query,
        )

    async def list_themes(
        self, **query: Unpack[qt.LegoThemesListQuery]
    ) -> ApiPage[ApiTheme]:
        return await self._page("lego_themes_list", ApiTheme, query=query)

    def iter_themes(
        self, **query: Unpack[qt.LegoThemesListQuery]
    ) -> AsyncIterator[ApiTheme]:
        return self._iter("lego_themes_list", ApiTheme, query=query)

    async def get_theme(
        self, theme_id: int, **query: Unpack[qt.LegoThemesReadQuery]
    ) -> ApiTheme:
        return await self._model(
            "lego_themes_read", ApiTheme, path={"id": theme_id}, query=query
        )

    async def get_openapi_spec(self) -> ApiRecord:
        return await self._model("swagger_list", ApiRecord)

    # Authentication and user data ---------------------------------------------
    async def create_user_token(self, payload: CreateUserTokenRequest) -> ApiUserToken:
        return await self._model(
            "users__token_create", ApiUserToken, form=payload.form()
        )

    async def list_badges(
        self, **query: Unpack[qt.UsersBadgesListQuery]
    ) -> ApiPage[ApiBadge]:
        return await self._page("users_badges_list", ApiBadge, query=query)

    def iter_badges(
        self, **query: Unpack[qt.UsersBadgesListQuery]
    ) -> AsyncIterator[ApiBadge]:
        return self._iter("users_badges_list", ApiBadge, query=query)

    async def get_badge(
        self, badge_id: int, **query: Unpack[qt.UsersBadgesReadQuery]
    ) -> ApiBadge:
        return await self._model(
            "users_badges_read", ApiBadge, path={"id": badge_id}, query=query
        )

    async def list_user_all_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersAllpartsListQuery],
    ) -> ApiPage[ApiUserPart]:
        return await self._page(
            "users_allparts_list",
            ApiUserPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_all_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersAllpartsListQuery],
    ) -> AsyncIterator[ApiUserPart]:
        return self._iter(
            "users_allparts_list",
            ApiUserPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def get_user_build_requirements(
        self, set_num: str, *, user_token: str | None = None
    ) -> ApiBuildResult:
        return await self._model(
            "users_build_read",
            ApiBuildResult,
            path={"user_token": self._token(user_token), "set_num": set_num},
        )

    async def list_user_lost_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersLostPartsListQuery],
    ) -> ApiPage[ApiLostPart]:
        return await self._page(
            "users_lost_parts_list",
            ApiLostPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_lost_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersLostPartsListQuery],
    ) -> AsyncIterator[ApiLostPart]:
        return self._iter(
            "users_lost_parts_list",
            ApiLostPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def add_user_lost_parts(
        self, payload: LostPartRequest, *, user_token: str | None = None
    ) -> ApiLostPart:
        return await self._model(
            "users_lost_parts_create",
            ApiLostPart,
            path={"user_token": self._token(user_token)},
            form=payload.form(),
        )

    async def delete_user_lost_part(
        self, lost_part_id: int, *, user_token: str | None = None
    ) -> ApiRecord:
        return await self._model(
            "users_lost_parts_delete",
            ApiRecord,
            path={"user_token": self._token(user_token), "id": lost_part_id},
        )

    async def list_user_minifigs(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersMinifigsListQuery],
    ) -> ApiPage[ApiUserMinifig]:
        return await self._page(
            "users_minifigs_list",
            ApiUserMinifig,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_minifigs(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersMinifigsListQuery],
    ) -> AsyncIterator[ApiUserMinifig]:
        return self._iter(
            "users_minifigs_list",
            ApiUserMinifig,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def list_user_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartsListQuery],
    ) -> ApiPage[ApiUserPart]:
        return await self._page(
            "users_parts_list",
            ApiUserPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_parts(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartsListQuery],
    ) -> AsyncIterator[ApiUserPart]:
        return self._iter(
            "users_parts_list",
            ApiUserPart,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def get_user_profile(self, *, user_token: str | None = None) -> ApiProfile:
        return await self._model(
            "users_profile_read",
            ApiProfile,
            path={"user_token": self._token(user_token)},
        )

    # Part lists ----------------------------------------------------------------
    async def list_user_part_lists(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsListQuery],
    ) -> ApiPage[ApiPartList]:
        return await self._page(
            "users_partlists_list",
            ApiPartList,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_part_lists(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsListQuery],
    ) -> AsyncIterator[ApiPartList]:
        return self._iter(
            "users_partlists_list",
            ApiPartList,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def create_user_part_list(
        self, payload: PartListRequest, *, user_token: str | None = None
    ) -> ApiPartList:
        return await self._model(
            "users_partlists_create",
            ApiPartList,
            path={"user_token": self._token(user_token)},
            form=payload.form(),
        )

    async def get_user_part_list(
        self, list_id: int, *, user_token: str | None = None
    ) -> ApiPartList:
        return await self._model(
            "users_partlists_read",
            ApiPartList,
            path={"user_token": self._token(user_token), "list_id": list_id},
        )

    async def replace_user_part_list(
        self, list_id: int, payload: PartListRequest, *, user_token: str | None = None
    ) -> ApiPartList:
        return await self._model(
            "users_partlists_update",
            ApiPartList,
            path={"user_token": self._token(user_token), "list_id": list_id},
            form=payload.form(),
        )

    async def update_user_part_list(
        self,
        list_id: int,
        payload: PartListUpdateRequest,
        *,
        user_token: str | None = None,
    ) -> ApiPartList:
        return await self._model(
            "users_partlists_partial_update",
            ApiPartList,
            path={"user_token": self._token(user_token), "list_id": list_id},
            form=payload.form(),
        )

    async def delete_user_part_list(
        self, list_id: int, *, user_token: str | None = None
    ) -> ApiRecord:
        return await self._model(
            "users_partlists_delete",
            ApiRecord,
            path={"user_token": self._token(user_token), "list_id": list_id},
        )

    async def list_user_part_list_parts(
        self,
        list_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsPartsListQuery],
    ) -> ApiPage[ApiPartListPart]:
        return await self._page(
            "users_partlists_parts_list",
            ApiPartListPart,
            path={"user_token": self._token(user_token), "list_id": list_id},
            query=query,
        )

    def iter_user_part_list_parts(
        self,
        list_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsPartsListQuery],
    ) -> AsyncIterator[ApiPartListPart]:
        return self._iter(
            "users_partlists_parts_list",
            ApiPartListPart,
            path={"user_token": self._token(user_token), "list_id": list_id},
            query=query,
        )

    async def add_user_part_list_parts(
        self,
        list_id: int,
        payload: PartListPartRequest | Sequence[PartListPartRequest],
        *,
        user_token: str | None = None,
    ) -> MutationResult:
        path = {
            "user_token": self._token(user_token),
            "list_id": list_id,
        }
        if isinstance(payload, PartListPartRequest):
            requested = ApiRecord.model_validate(payload.form())
            accepted = await self._model(
                "users_partlists_parts_create",
                ApiRecord,
                path=path,
                form=payload.form(),
            )
            return MutationResult(requested=(requested,), accepted=(accepted,))
        return await self._mutation(
            "users_partlists_parts_create",
            path=path,
            json_body=[item.form() for item in payload],
        )

    async def add_user_part_list_parts_sequential(
        self,
        list_id: int,
        payload: Sequence[PartListPartRequest],
        *,
        user_token: str | None = None,
    ) -> MutationResult:
        """Add items one request at a time and retain partial-failure details."""
        items = tuple(payload)
        requested = tuple(ApiRecord.model_validate(item.form()) for item in items)
        accepted: list[ApiRecord] = []
        for index, item in enumerate(items):
            try:
                accepted.append(
                    await self._model(
                        "users_partlists_parts_create",
                        ApiRecord,
                        path={
                            "user_token": self._token(user_token),
                            "list_id": list_id,
                        },
                        form=item.form(),
                    )
                )
            except ApiError as exc:
                raise BatchMutationError(
                    "users_partlists_parts_create", tuple(accepted), index
                ) from exc
        return MutationResult(requested=requested, accepted=tuple(accepted))

    async def get_user_part_list_part(
        self,
        list_id: int,
        part_num: str,
        color_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsPartsReadQuery],
    ) -> ApiPartListPart:
        return await self._model(
            "users_partlists_parts_read",
            ApiPartListPart,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "part_num": part_num,
                "color_id": color_id,
            },
            query=query,
        )

    async def replace_user_part_list_part(
        self,
        list_id: int,
        part_num: str,
        color_id: int,
        payload: QuantityRequest,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsPartsUpdateQuery],
    ) -> ApiPartListPart:
        return await self._model(
            "users_partlists_parts_update",
            ApiPartListPart,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "part_num": part_num,
                "color_id": color_id,
            },
            query=query,
            form=payload.form(),
        )

    async def delete_user_part_list_part(
        self,
        list_id: int,
        part_num: str,
        color_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersPartlistsPartsDeleteQuery],
    ) -> ApiRecord:
        return await self._model(
            "users_partlists_parts_delete",
            ApiRecord,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "part_num": part_num,
                "color_id": color_id,
            },
            query=query,
        )

    # Set lists and collection sets --------------------------------------------
    async def list_user_set_lists(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsListQuery],
    ) -> ApiPage[ApiSetList]:
        return await self._page(
            "users_setlists_list",
            ApiSetList,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_set_lists(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsListQuery],
    ) -> AsyncIterator[ApiSetList]:
        return self._iter(
            "users_setlists_list",
            ApiSetList,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def create_user_set_list(
        self, payload: SetListRequest, *, user_token: str | None = None
    ) -> ApiSetList:
        return await self._model(
            "users_setlists_create",
            ApiSetList,
            path={"user_token": self._token(user_token)},
            form=payload.form(),
        )

    async def get_user_set_list(
        self, list_id: int, *, user_token: str | None = None
    ) -> ApiSetList:
        return await self._model(
            "users_setlists_read",
            ApiSetList,
            path={"user_token": self._token(user_token), "list_id": list_id},
        )

    async def replace_user_set_list(
        self, list_id: int, payload: SetListRequest, *, user_token: str | None = None
    ) -> ApiSetList:
        return await self._model(
            "users_setlists_update",
            ApiSetList,
            path={"user_token": self._token(user_token), "list_id": list_id},
            form=payload.form(),
        )

    async def update_user_set_list(
        self,
        list_id: int,
        payload: SetListUpdateRequest,
        *,
        user_token: str | None = None,
    ) -> ApiSetList:
        return await self._model(
            "users_setlists_partial_update",
            ApiSetList,
            path={"user_token": self._token(user_token), "list_id": list_id},
            form=payload.form(),
        )

    async def delete_user_set_list(
        self, list_id: int, *, user_token: str | None = None
    ) -> ApiRecord:
        return await self._model(
            "users_setlists_delete",
            ApiRecord,
            path={"user_token": self._token(user_token), "list_id": list_id},
        )

    async def list_user_set_list_sets(
        self,
        list_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsListQuery],
    ) -> ApiPage[ApiSetListSet]:
        return await self._page(
            "users_setlists_sets_list",
            ApiSetListSet,
            path={"user_token": self._token(user_token), "list_id": list_id},
            query=query,
        )

    def iter_user_set_list_sets(
        self,
        list_id: int,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsListQuery],
    ) -> AsyncIterator[ApiSetListSet]:
        return self._iter(
            "users_setlists_sets_list",
            ApiSetListSet,
            path={"user_token": self._token(user_token), "list_id": list_id},
            query=query,
        )

    async def add_user_set_list_sets(
        self,
        list_id: int,
        payload: SetQuantityRequest | Sequence[SetQuantityRequest],
        *,
        user_token: str | None = None,
    ) -> MutationResult:
        path = {
            "user_token": self._token(user_token),
            "list_id": list_id,
        }
        if isinstance(payload, SetQuantityRequest):
            requested = ApiRecord.model_validate(payload.form())
            accepted = await self._model(
                "users_setlists_sets_create",
                ApiRecord,
                path=path,
                form=payload.form(),
            )
            return MutationResult(requested=(requested,), accepted=(accepted,))
        return await self._mutation(
            "users_setlists_sets_create",
            path=path,
            json_body=[item.form() for item in payload],
        )

    async def add_user_set_list_sets_sequential(
        self,
        list_id: int,
        payload: Sequence[SetQuantityRequest],
        *,
        user_token: str | None = None,
    ) -> MutationResult:
        """Add sets one request at a time and retain partial-failure details."""
        items = tuple(payload)
        requested = tuple(ApiRecord.model_validate(item.form()) for item in items)
        accepted: list[ApiRecord] = []
        for index, item in enumerate(items):
            try:
                accepted.append(
                    await self._model(
                        "users_setlists_sets_create",
                        ApiRecord,
                        path={
                            "user_token": self._token(user_token),
                            "list_id": list_id,
                        },
                        form=item.form(),
                    )
                )
            except ApiError as exc:
                raise BatchMutationError(
                    "users_setlists_sets_create", tuple(accepted), index
                ) from exc
        return MutationResult(requested=requested, accepted=tuple(accepted))

    async def get_user_set_list_set(
        self,
        list_id: int,
        set_num: str,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsReadQuery],
    ) -> ApiSetListSet:
        return await self._model(
            "users_setlists_sets_read",
            ApiSetListSet,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "set_num": set_num,
            },
            query=query,
        )

    async def replace_user_set_list_set(
        self,
        list_id: int,
        set_num: str,
        payload: SetListSetUpdateRequest,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsUpdateQuery],
    ) -> ApiSetListSet:
        return await self._model(
            "users_setlists_sets_update",
            ApiSetListSet,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "set_num": set_num,
            },
            query=query,
            form=payload.form(),
        )

    async def update_user_set_list_set(
        self,
        list_id: int,
        set_num: str,
        payload: SetListSetUpdateRequest,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsPartialUpdateQuery],
    ) -> ApiSetListSet:
        return await self._model(
            "users_setlists_sets_partial_update",
            ApiSetListSet,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "set_num": set_num,
            },
            query=query,
            form=payload.form(),
        )

    async def delete_user_set_list_set(
        self,
        list_id: int,
        set_num: str,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetlistsSetsDeleteQuery],
    ) -> ApiRecord:
        return await self._model(
            "users_setlists_sets_delete",
            ApiRecord,
            path={
                "user_token": self._token(user_token),
                "list_id": list_id,
                "set_num": set_num,
            },
            query=query,
        )

    async def list_user_sets(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetsListQuery],
    ) -> ApiPage[ApiUserSet]:
        return await self._page(
            "users_sets_list",
            ApiUserSet,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    def iter_user_sets(
        self,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetsListQuery],
    ) -> AsyncIterator[ApiUserSet]:
        return self._iter(
            "users_sets_list",
            ApiUserSet,
            path={"user_token": self._token(user_token)},
            query=query,
        )

    async def add_user_sets(
        self,
        payload: SetQuantityRequest | Sequence[SetQuantityRequest],
        *,
        user_token: str | None = None,
    ) -> MutationResult:
        path = {"user_token": self._token(user_token)}
        if isinstance(payload, SetQuantityRequest):
            requested = ApiRecord.model_validate(payload.form())
            record = await self._model(
                "users_sets_create", ApiRecord, path=path, form=payload.form()
            )
            return MutationResult(requested=(requested,), accepted=(record,))
        return await self._mutation(
            "users_sets_create",
            path=path,
            json_body=[item.form() for item in payload],
        )

    async def sync_user_sets(
        self,
        payload: UserSetsSyncRequest,
        *,
        confirm_replace: bool = False,
        user_token: str | None = None,
    ) -> MutationResult:
        if not confirm_replace:
            raise ValueError("sync_user_sets requires confirm_replace=True")
        return await self._mutation(
            "users_sets_sync_create",
            path={"user_token": self._token(user_token)},
            json_body=[item.form() for item in payload.sets],
        )

    async def get_user_set(
        self,
        set_num: str,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetsReadQuery],
    ) -> ApiUserSet:
        return await self._model(
            "users_sets_read",
            ApiUserSet,
            path={"user_token": self._token(user_token), "set_num": set_num},
            query=query,
        )

    async def set_user_set_quantity(
        self,
        set_num: str,
        payload: QuantityRequest,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetsUpdateQuery],
    ) -> ApiUserSet:
        return await self._model(
            "users_sets_update",
            ApiUserSet,
            path={"user_token": self._token(user_token), "set_num": set_num},
            query=query,
            form=payload.form(),
        )

    async def delete_user_set(
        self,
        set_num: str,
        *,
        user_token: str | None = None,
        **query: Unpack[qt.UsersSetsDeleteQuery],
    ) -> ApiRecord:
        return await self._model(
            "users_sets_delete",
            ApiRecord,
            path={"user_token": self._token(user_token), "set_num": set_num},
            query=query,
        )

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(OPERATIONS)
