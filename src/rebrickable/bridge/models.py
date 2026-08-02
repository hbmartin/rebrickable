"""Neutral LDraw mapping and translation records."""

from __future__ import annotations

from dataclasses import dataclass

from rebrickable.types import MappingSource, MappingStatus


@dataclass(frozen=True, slots=True)
class LDrawBomItem:
    part_num: str
    color_code: int
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")

    @classmethod
    def from_pyldraw_bom(cls, row: object) -> LDrawBomItem:
        try:
            part = str(getattr(row, "part"))
            color = getattr(row, "colour_code")
            quantity = int(getattr(row, "quantity"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("not a pyldraw3 BomRow") from exc
        if color is None:
            raise ValueError("LDraw BOM row has no color code")
        return cls(part, int(color), quantity)


@dataclass(frozen=True, slots=True)
class LDrawColorInfo:
    code: int
    name: str
    rgb: str
    alpha: int = 255

    @classmethod
    def from_pyldraw_colour(cls, colour: object) -> LDrawColorInfo:
        try:
            code = getattr(colour, "code")
            name = getattr(colour, "name")
            rgb = getattr(colour, "rgb")
            alpha = getattr(colour, "alpha")
        except AttributeError as exc:
            raise ValueError("not a pyldraw3 Colour") from exc
        if code is None or name is None or rgb is None:
            raise ValueError("pyldraw3 Colour lacks code, name, or RGB")
        return cls(int(code), str(name), str(rgb), 255 if alpha is None else int(alpha))


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    identifier: str
    name: str | None
    reason: str
    confidence: float


@dataclass(frozen=True, slots=True)
class PartMatch:
    source_identifier: str
    target_identifier: str | None
    status: MappingStatus
    source: MappingSource
    confidence: float
    candidates: tuple[MatchCandidate, ...]
    explanation: str
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class ColorMatch:
    source_identifier: int
    target_identifier: int | None
    status: MappingStatus
    source: MappingSource
    confidence: float
    candidates: tuple[MatchCandidate, ...]
    explanation: str
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class TranslatedBomRow:
    ldraw_part_num: str
    ldraw_color_code: int
    rebrickable_part_num: str | None
    rebrickable_color_id: int | None
    quantity: int
    status: MappingStatus
    part_match: PartMatch
    color_match: ColorMatch


@dataclass(frozen=True, slots=True)
class TranslationReport:
    rows: tuple[TranslatedBomRow, ...]
    resolved_count: int
    ambiguous_count: int
    unresolved_count: int
    snapshot_id: str
    diagnostics: tuple[str, ...] = ()

    @property
    def ambiguous_rows(self) -> tuple[TranslatedBomRow, ...]:
        return tuple(row for row in self.rows if row.status is MappingStatus.AMBIGUOUS)

    @property
    def unresolved_rows(self) -> tuple[TranslatedBomRow, ...]:
        return tuple(row for row in self.rows if row.status is MappingStatus.UNRESOLVED)

    @property
    def incomplete_rows(self) -> tuple[TranslatedBomRow, ...]:
        """Every row still needing review: ambiguous or unresolved."""
        return tuple(
            row for row in self.rows if row.status is not MappingStatus.RESOLVED
        )
