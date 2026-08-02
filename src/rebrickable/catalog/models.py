"""Stable local catalog domain models."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, overload

from rebrickable.types import RelationshipType
from rebrickable.urls import minifig_url, part_url, set_url, theme_url

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class Theme:
    id: int
    name: str
    parent_id: int | None

    @property
    def page_url(self) -> str:
        return theme_url(self.id)


@dataclass(frozen=True, slots=True)
class Color:
    id: int
    name: str
    rgb: str
    is_trans: bool
    num_parts: int
    num_sets: int
    year_from: int | None
    year_to: int | None


@dataclass(frozen=True, slots=True)
class PartCategory:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Part:
    part_num: str
    name: str
    category_id: int
    material: str

    @property
    def page_url(self) -> str:
        return part_url(self.part_num)


@dataclass(frozen=True, slots=True)
class PartRelationship:
    type: RelationshipType
    raw_type: str
    child_part_num: str
    parent_part_num: str


@dataclass(frozen=True, slots=True)
class Element:
    element_id: str
    part_num: str
    color_id: int
    design_id: str


@dataclass(frozen=True, slots=True)
class Set:
    set_num: str
    name: str
    year: int
    theme_id: int
    num_parts: int
    image_url: str | None

    @property
    def page_url(self) -> str:
        return set_url(self.set_num)


@dataclass(frozen=True, slots=True)
class Minifig:
    fig_num: str
    name: str
    num_parts: int
    image_url: str | None

    @property
    def page_url(self) -> str:
        return minifig_url(self.fig_num)


@dataclass(frozen=True, slots=True)
class InventoryPart:
    part: Part
    color: Color
    quantity: int
    is_spare: bool
    element_ids: tuple[str, ...]
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class InventorySet:
    set: Set
    quantity: int


@dataclass(frozen=True, slots=True)
class InventoryMinifig:
    minifig: Minifig
    quantity: int


@dataclass(frozen=True, slots=True)
class Inventory:
    owner_num: str
    version: int
    parts: tuple[InventoryPart, ...]
    sets: tuple[InventorySet, ...]
    minifigs: tuple[InventoryMinifig, ...]


@dataclass(frozen=True, slots=True)
class BomContribution:
    quantity: int
    owner_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BomRow:
    part: Part
    color: Color
    quantity: int
    provenance: tuple[BomContribution, ...]


@dataclass(frozen=True, slots=True)
class SkippedInventory:
    """A contained set or minifig whose own inventory could not be expanded."""

    owner_num: str
    owner_path: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogBom(Sequence[BomRow]):
    """Recursive BOM rows plus diagnostics for unexpandable sub-items."""

    rows: tuple[BomRow, ...]
    skipped: tuple[SkippedInventory, ...] = ()

    def __iter__(self) -> Iterator[BomRow]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    @overload
    def __getitem__(self, index: int) -> BomRow: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[BomRow, ...]: ...

    def __getitem__(self, index: int | slice) -> BomRow | tuple[BomRow, ...]:
        return self.rows[index]
