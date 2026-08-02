"""Namespace-aware deterministic bills of materials."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree

from rebrickable.exports import escape_csv_formula
from rebrickable.types import (
    ColorRef,
    ColorSystem,
    PartRef,
    PartSystem,
    SubstitutionEvidence,
)


@dataclass(frozen=True, slots=True)
class BomItem:
    part: PartRef
    color: ColorRef
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("BOM quantity must be positive")


@dataclass(frozen=True, slots=True)
class DuplicateBomRow:
    part: PartRef
    color: ColorRef
    occurrences: int


@dataclass(frozen=True, slots=True)
class BomDiffRow:
    part: PartRef
    color: ColorRef
    before: int
    after: int

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class BomDiff:
    rows: tuple[BomDiffRow, ...]


@dataclass(frozen=True, slots=True)
class BomValidationRow:
    item: BomItem
    available: bool
    rebrickable_part: str | None
    rebrickable_color: int | None
    confidence: str
    notes: tuple[str, ...] = ()
    substitutions: tuple[SubstitutionEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class BomValidationReport:
    rows: tuple[BomValidationRow, ...]
    total_quantity: int
    unique_count: int
    exact_count: int
    unavailable_count: int
    substitutable_count: int = 0


@dataclass(frozen=True, slots=True)
class Bom:
    items: tuple[BomItem, ...]
    duplicate_rows: tuple[DuplicateBomRow, ...] = ()

    @classmethod
    def from_csv(
        cls,
        source: str | Path,
        *,
        part_system: PartSystem | str = PartSystem.LDRAW,
        color_system: ColorSystem | str = ColorSystem.LDRAW,
    ) -> Bom:
        text = _read_source(source, encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("BOM CSV has no header")
        lookup = {name.casefold(): name for name in reader.fieldnames}
        required = {"part", "color", "quantity"}
        if not required <= set(lookup):
            raise ValueError("BOM CSV requires Part,Color,Quantity")
        raw: list[BomItem] = []
        for number, row in enumerate(reader, start=2):
            try:
                raw.append(
                    BomItem(
                        PartRef(part_system, row[lookup["part"]]),
                        ColorRef(color_system, int(row[lookup["color"]])),
                        int(row[lookup["quantity"]]),
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid BOM CSV row {number}: {exc}") from exc
        return cls.normalize(raw)

    @classmethod
    def from_rebrickable_xml(cls, source: str | Path) -> Bom:
        text = _read_source(source, encoding="utf-8")
        root = ElementTree.fromstring(text)
        items: list[BomItem] = []
        for element in root.findall(".//ITEM"):
            part = element.findtext("ITEMID") or element.findtext("PART")
            color = element.findtext("COLOR")
            quantity = element.findtext("MINQTY") or element.findtext("QTY")
            if part is None or color is None or quantity is None:
                raise ValueError("Rebrickable XML item lacks part, color, or quantity")
            items.append(
                BomItem(
                    PartRef(PartSystem.REBRICKABLE, part),
                    ColorRef(ColorSystem.REBRICKABLE, int(color)),
                    int(quantity),
                ),
            )
        return cls.normalize(items)

    @classmethod
    def from_rebrickable_csv(cls, source: str | Path) -> Bom:
        """Parse Rebrickable inventory/custom-list CSV column spellings."""
        text = _read_source(source, encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValueError("Rebrickable CSV has no header")
        lookup = {name.strip().casefold(): name for name in reader.fieldnames}

        def column(*names: str) -> str:
            for name in names:
                if name in lookup:
                    return lookup[name]
            raise ValueError(
                "Rebrickable CSV requires part_num/Part, color_id/Color, and quantity"
            )

        part_column = column("part_num", "part")
        color_column = column("color_id", "color")
        quantity_column = column("quantity", "qty", "minqty")
        rows: list[BomItem] = []
        for number, row in enumerate(reader, start=2):
            try:
                rows.append(
                    BomItem(
                        PartRef(PartSystem.REBRICKABLE, row[part_column]),
                        ColorRef(
                            ColorSystem.REBRICKABLE,
                            int(row[color_column]),
                        ),
                        int(row[quantity_column]),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid Rebrickable CSV row {number}: {exc}"
                ) from exc
        return cls.normalize(rows)

    @classmethod
    def normalize(cls, rows: list[BomItem] | tuple[BomItem, ...]) -> Bom:
        quantities: defaultdict[tuple[PartRef, ColorRef], int] = defaultdict(int)
        occurrences: defaultdict[tuple[PartRef, ColorRef], int] = defaultdict(int)
        for row in rows:
            key = (row.part, row.color)
            quantities[key] += row.quantity
            occurrences[key] += 1
        ordered = sorted(
            quantities,
            key=lambda key: (
                key[0].system.value,
                key[0].value,
                key[1].system.value,
                key[1].value,
            ),
        )
        return cls(
            tuple(
                BomItem(part, color, quantities[(part, color)])
                for part, color in ordered
            ),
            tuple(
                DuplicateBomRow(part, color, occurrences[(part, color)])
                for part, color in ordered
                if occurrences[(part, color)] > 1
            ),
        )

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def unique_count(self) -> int:
        return len(self.items)

    def diff(self, other: Bom) -> BomDiff:
        before = {(item.part, item.color): item.quantity for item in self.items}
        after = {(item.part, item.color): item.quantity for item in other.items}
        keys = sorted(
            before.keys() | after.keys(),
            key=lambda key: (
                key[0].system.value,
                key[0].value,
                key[1].system.value,
                key[1].value,
            ),
        )
        return BomDiff(
            tuple(
                BomDiffRow(
                    part,
                    color,
                    before.get((part, color), 0),
                    after.get((part, color), 0),
                )
                for part, color in keys
                if before.get((part, color), 0) != after.get((part, color), 0)
            ),
        )

    def to_rebrickable_csv(self) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(("part_num", "color_id", "quantity"))
        for item in self.items:
            if (
                item.part.system is not PartSystem.REBRICKABLE
                or item.color.system is not ColorSystem.REBRICKABLE
            ):
                raise ValueError("custom-list export requires Rebrickable identifiers")
            writer.writerow(
                (escape_csv_formula(item.part.value), item.color.value, item.quantity)
            )
        return output.getvalue()


def _read_source(source: str | Path, *, encoding: str) -> str:
    if isinstance(source, Path):
        return source.read_text(encoding=encoding)
    if "\n" in source or "\r" in source:
        return source
    looks_like_path = source.casefold().endswith((".csv", ".xml"))
    try:
        path = Path(source)
        if path.is_file():
            return path.read_text(encoding=encoding)
    except OSError:
        if looks_like_path:
            raise
        return source
    if looks_like_path:
        raise FileNotFoundError(f"BOM source file not found: {source}")
    return source
