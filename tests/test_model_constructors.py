"""Public construction and serialization contracts for domain values."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, asdict, is_dataclass

import pytest

import rebrickable
from rebrickable import ColorRef, LDrawBomItem, PartRef


@pytest.mark.parametrize(
    "model",
    [
        model
        for name in rebrickable.__all__
        if inspect.isclass(model := getattr(rebrickable, name)) and is_dataclass(model)
    ],
)
def test_public_dataclass_construction_requires_keywords(model: type[object]) -> None:
    with pytest.raises(TypeError, match="positional"):
        model(object())


def test_keyword_construction_preserves_normalization_and_value_semantics() -> None:
    part = PartRef(system="ldraw", value=" 3001.DAT ")
    color = ColorRef(system="ldraw", value="4")
    assert part == PartRef(system="ldraw", value="3001")
    assert hash(part) == hash(PartRef(system="ldraw", value="3001"))
    assert color == ColorRef(system="ldraw", value=4)

    item = LDrawBomItem(part_num="3001", color_code=4, quantity=2)
    assert asdict(item) == {"part_num": "3001", "color_code": 4, "quantity": 2}
    with pytest.raises(FrozenInstanceError):
        setattr(item, "quantity", 3)  # noqa: B010 - exercise frozen value behavior
    match item:
        case LDrawBomItem(part_num="3001", color_code=4, quantity=2):
            pass
        case _:
            pytest.fail("keyword class patterns must preserve the domain fields")
