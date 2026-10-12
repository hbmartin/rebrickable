"""Public construction and serialization contracts for domain values."""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError, asdict, is_dataclass
from pathlib import Path

import pytest

import rebrickable
from rebrickable import ApiError, ColorRef, Config, LDrawBomItem, PartRef
from rebrickable.progress import ProgressEvent, ProgressStage

_PUBLIC_MODELS = {
    name: model
    for name in rebrickable.__all__
    if inspect.isclass(model := getattr(rebrickable, name)) and is_dataclass(model)
}
_PUBLIC_MODELS["ProgressEvent"] = ProgressEvent
# Captured from c66a005 before reconciling the Garuff constructor changes.
_CONTRACTS = json.loads(
    Path(__file__)
    .with_name("fixtures")
    .joinpath("public_constructors.json")
    .read_text()
)


@pytest.mark.parametrize(
    "model",
    _PUBLIC_MODELS.values(),
    ids=_PUBLIC_MODELS,
)
def test_public_constructor_signature_and_binding_remain_compatible(
    model: type[object],
) -> None:
    signature = inspect.signature(model)
    parameters = tuple(signature.parameters.values())
    contract = _CONTRACTS[model.__name__]
    assert [
        [
            parameter.name,
            parameter.kind.name,
            None
            if parameter.default is inspect.Parameter.empty
            else repr(parameter.default),
        ]
        for parameter in parameters
    ] == contract["parameters"]
    assert list(model.__match_args__) == contract["match_args"]

    keywords = {parameter.name: object() for parameter in parameters}
    assert tuple(signature.bind(**keywords).arguments) == tuple(keywords)
    positional = [
        keywords[parameter.name]
        for parameter in parameters
        if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    keyword_only = {
        parameter.name: keywords[parameter.name]
        for parameter in parameters
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert tuple(signature.bind(*positional, **keyword_only).arguments) == tuple(
        keywords
    )


def test_constructor_contract_covers_root_exports_and_progress() -> None:
    assert _CONTRACTS.keys() == _PUBLIC_MODELS.keys()


def test_positional_keyword_and_mixed_construction(tmp_path: Path) -> None:
    assert Config(tmp_path / "catalog.sqlite") == Config(
        database_path=tmp_path / "catalog.sqlite"
    )
    assert PartRef("ldraw", " 3001.DAT ") == PartRef(system="ldraw", value="3001")
    assert PartRef("ldraw", value="3001.dat") == PartRef("ldraw", "3001")
    assert ColorRef("ldraw", "4") == ColorRef(system="ldraw", value=4)
    assert ColorRef("ldraw", value=4) == ColorRef("ldraw", 4)
    assert LDrawBomItem("3001", 4, quantity=2) == LDrawBomItem(
        part_num="3001", color_code=4, quantity=2
    )
    assert ApiError(429, "/parts/", "parts", detail="slow down") == ApiError(
        status=429, path_template="/parts/", operation_id="parts", detail="slow down"
    )
    assert ProgressEvent(ProgressStage.DOWNLOAD, "parts", current=10) == ProgressEvent(
        stage=ProgressStage.DOWNLOAD, dataset="parts", current=10
    )


def test_positional_class_patterns_remain_compatible() -> None:
    match PartRef("ldraw", "3001.dat"):
        case PartRef("ldraw", "3001"):
            pass
        case _:
            pytest.fail("positional reference patterns must retain normalized fields")
    match LDrawBomItem("3001", 4, 2):
        case LDrawBomItem("3001", 4, 2):
            pass
        case _:
            pytest.fail("positional BOM patterns must retain field order")
    match ProgressEvent(ProgressStage.DOWNLOAD, "parts", 10):
        case ProgressEvent(ProgressStage.DOWNLOAD, "parts", 10):
            pass
        case _:
            pytest.fail("positional progress patterns must retain field order")


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
