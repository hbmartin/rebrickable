"""Hand-written API response DTOs and mutation payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from rebrickable.types import freeze_json

T = TypeVar("T")


class ApiModel(BaseModel):
    """Frozen DTO that preserves additive fields in immutable ``extra``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    extra: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def collect_extra(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        known = set(cls.model_fields)
        supplied = dict(value)
        existing = supplied.pop("extra", {})
        additions = {
            key: supplied.pop(key) for key in tuple(supplied) if key not in known
        }
        supplied["extra"] = freeze_json({**existing, **additions})
        return supplied


class ApiRecord(ApiModel):
    """Typed additive record for endpoints without published schemas."""


class ApiPage(ApiModel, Generic[T]):
    count: int
    next: HttpUrl | None = None
    previous: HttpUrl | None = None
    results: tuple[T, ...]


class ApiColor(ApiModel):
    id: int
    name: str
    rgb: str | None = None
    is_trans: bool | None = None
    external_ids: Mapping[str, Any] = Field(default_factory=dict)


class ApiPartCategory(ApiModel):
    id: int
    name: str


class ApiTheme(ApiModel):
    id: int
    name: str
    parent_id: int | None = None


class ApiPart(ApiModel):
    part_num: str
    name: str
    part_cat_id: int | None = None
    part_url: HttpUrl | None = None
    part_img_url: HttpUrl | None = None
    external_ids: Mapping[str, Any] = Field(default_factory=dict)


class ApiSet(ApiModel):
    set_num: str
    name: str
    year: int | None = None
    theme_id: int | None = None
    num_parts: int | None = None
    set_url: HttpUrl | None = None
    set_img_url: HttpUrl | None = None


class ApiMinifig(ApiModel):
    set_num: str
    name: str
    num_parts: int | None = None
    set_url: HttpUrl | None = None
    set_img_url: HttpUrl | None = None

    @property
    def fig_num(self) -> str:
        return self.set_num


class ApiElement(ApiModel):
    element_id: str
    part: ApiPart | None = None
    color: ApiColor | None = None
    design_id: str | None = None


class ApiPartColor(ApiModel):
    color_id: int | None = None
    color_name: str | None = None
    num_sets: int | None = None
    num_set_parts: int | None = None
    part_img_url: HttpUrl | None = None
    elements: tuple[str, ...] = ()


class ApiInventoryPart(ApiModel):
    id: int | None = None
    inv_part_id: int | None = None
    part: ApiPart
    color: ApiColor
    quantity: int
    is_spare: bool = False


class ApiInventorySet(ApiModel):
    set_num: str | None = None
    quantity: int = 1


class ApiInventoryMinifig(ApiModel):
    set_num: str | None = None
    quantity: int = 1


class ApiAlternateBuild(ApiModel):
    set_num: str | None = None
    name: str
    designer_name: str | None = None


class ApiUserToken(ApiModel):
    user_token: str = Field(repr=False)


class ApiBadge(ApiModel):
    id: int
    name: str


class ApiProfile(ApiModel):
    username: str
    email: str | None = None
    avatar_img: HttpUrl | None = None


class ApiBuildResult(ApiModel):
    can_build: bool | None = None
    num_missing: int | None = None


class ApiLostPart(ApiModel):
    id: int
    lost_quantity: int | None = None


class ApiUserPart(ApiModel):
    part: ApiPart
    color: ApiColor
    quantity: int


class ApiUserMinifig(ApiModel):
    set_num: str
    quantity: int = 1


class ApiUserSet(ApiModel):
    set_num: str
    quantity: int = 1
    include_spares: bool = False


class ApiPartList(ApiModel):
    id: int
    name: str
    is_buildable: bool | None = None
    num_parts: int | None = None


class ApiPartListPart(ApiModel):
    part: ApiPart | None = None
    part_num: str | None = None
    color: ApiColor | None = None
    color_id: int | None = None
    quantity: int


class ApiSetList(ApiModel):
    id: int
    name: str
    is_buildable: bool | None = None
    num_sets: int | None = None


class ApiSetListSet(ApiModel):
    set_num: str
    quantity: int = 1
    include_spares: bool = False


class MutationResult(ApiModel):
    accepted: tuple[ApiRecord, ...] = ()
    skipped: tuple[ApiRecord, ...] = ()


class RequestModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def form(self) -> dict[str, str | int | bool]:
        return self.model_dump(exclude_none=True)


class CreateUserTokenRequest(RequestModel):
    username: str
    password: str = Field(repr=False)


class LostPartRequest(RequestModel):
    inv_part_id: int
    lost_quantity: int | None = None


class PartListRequest(RequestModel):
    name: str
    is_buildable: bool | None = None
    num_parts: int | None = None


class PartListUpdateRequest(RequestModel):
    name: str | None = None
    is_buildable: bool | None = None
    num_parts: int | None = None


class PartListPartRequest(RequestModel):
    part_num: str
    color_id: int
    quantity: int


class QuantityRequest(RequestModel):
    quantity: int


class SetListRequest(RequestModel):
    name: str
    is_buildable: bool | None = None
    num_sets: int | None = None


class SetListUpdateRequest(RequestModel):
    name: str | None = None
    is_buildable: bool | None = None
    num_sets: int | None = None


class SetQuantityRequest(RequestModel):
    set_num: str | None = None
    quantity: int = 1
    include_spares: bool = False


class UserSetsSyncRequest(RequestModel):
    sets: tuple[SetQuantityRequest, ...]
