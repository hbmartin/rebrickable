"""Shared immutable public types and enums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | MappingProxyType


def normalize_ldraw_code(value: str) -> str:
    """Normalize an LDraw part reference for comparison and storage."""
    normalized = value.replace("\\", "/").casefold().strip()
    return normalized.removesuffix(".dat")


class CatalogStatus(StrEnum):
    READY = "ready"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SCHEMA_MISMATCH = "schema_mismatch"
    IMPORT_REQUIRED = "import_required"


class RefreshOutcome(StrEnum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class SearchKind(StrEnum):
    PART = "part"
    SET = "set"
    MINIFIG = "minifig"
    THEME = "theme"
    PART_CATEGORY = "part_category"
    COLOR = "color"
    ELEMENT = "element"


class MappingStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class MappingSource(StrEnum):
    USER_OVERRIDE = "user_override"
    API_EXTERNAL_ID = "api_external_id"
    EXACT_CANONICAL_ID = "exact_canonical_id"
    RELATIONSHIP_CANDIDATE = "relationship_candidate"
    RGB = "rgb"
    NAME = "name"
    NONE = "none"


class ProvenanceSource(StrEnum):
    LOCAL_SNAPSHOT = "local_snapshot"
    API_OPERATION = "api_operation"
    USER_OVERRIDE = "user_override"
    MAPPING_STRATEGY = "mapping_strategy"


class PartSystem(StrEnum):
    LDRAW = "ldraw"
    REBRICKABLE = "rebrickable"
    LEGO = "lego"
    BRICKLINK = "bricklink"
    BRICKOWL = "brickowl"


class ColorSystem(StrEnum):
    LDRAW = "ldraw"
    REBRICKABLE = "rebrickable"
    BRICKLINK = "bricklink"
    BRICKOWL = "brickowl"


class RelationshipType(StrEnum):
    PRINT = "P"
    PAIR = "R"
    SUB_PART = "B"
    MOLD = "M"
    PATTERN = "T"
    ALTERNATE = "A"
    UNKNOWN = "?"

    @classmethod
    def from_upstream(cls, value: str) -> RelationshipType:
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class ExitCode(IntEnum):
    OK = 0
    UNEXPECTED = 1
    INVALID_INPUT = 2
    MISSING_DATA = 3
    INCOMPLETE_TRANSLATION = 4
    API_FAILURE = 5


@dataclass(frozen=True, slots=True)
class PartRef:
    system: PartSystem
    value: str

    def __init__(self, system: PartSystem | str, value: str) -> None:
        resolved = PartSystem(system)
        if resolved is PartSystem.LDRAW:
            value = normalize_ldraw_code(value)
        object.__setattr__(self, "system", resolved)
        object.__setattr__(self, "value", value)
        if not value:
            raise ValueError("part identifier must not be empty")


@dataclass(frozen=True, slots=True)
class ColorRef:
    system: ColorSystem
    value: int

    def __init__(self, system: ColorSystem | str, value: int | str) -> None:
        object.__setattr__(self, "system", ColorSystem(system))
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError as exc:
                raise ValueError(
                    f"color identifier must be an integer: {value!r}"
                ) from exc
        object.__setattr__(self, "value", int(value))


@dataclass(frozen=True, slots=True)
class Provenance:
    source: ProvenanceSource
    reference: str
    retrieved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    dataset: str
    url: str
    etag: str | None
    last_modified: str | None
    byte_length: int
    sha256: str
    row_count: int
    unknown_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogState:
    status: CatalogStatus
    database_path: Path
    manifest_path: Path
    snapshot_id: str | None = None
    retrieved_at: datetime | None = None
    files: tuple[FileFingerprint, ...] = ()
    schema_version: int | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RefreshReport:
    outcome: RefreshOutcome
    snapshot_id: str
    retrieved_at: datetime
    files: tuple[FileFingerprint, ...]


@dataclass(frozen=True, slots=True)
class SearchFilters:
    year_from: int | None = None
    year_to: int | None = None
    theme_id: int | None = None
    min_parts: int | None = None
    max_parts: int | None = None
    category_id: int | None = None
    material: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    kind: SearchKind
    canonical_id: str
    title: str
    subtitle: str | None
    score: float
    matched_field: str
    matched_value: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    total: int
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class PartColorAvailability:
    available: bool
    rebrickable_part: str | None
    rebrickable_color: int | None
    element_ids: tuple[str, ...] = ()
    set_count: int = 0
    inventory_count: int = 0
    first_year: int | None = None
    last_year: int | None = None
    appears_as_spare: bool = False
    spare_only: bool = False
    relationship_only: bool = False
    confidence: str = "none"
    provenance: tuple[Provenance, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalIdentifier:
    system: str
    value: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class SubstitutionEvidence:
    source: PartRef
    candidate: PartRef
    relationship: RelationshipType
    raw_relationship: str
    path: tuple[PartRef, ...]
    changes_appearance: bool
    changes_geometry: bool
    explanation: str
    provenance: Provenance


def freeze_json(value: Any) -> Any:
    """Recursively freeze JSON-compatible data for immutable DTO extras."""
    if isinstance(value, dict):
        return MappingProxyType({str(k): freeze_json(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_json(item) for item in value)
    return value
