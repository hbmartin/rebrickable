from __future__ import annotations

import json
from pathlib import Path

from rebrickable import cli
from rebrickable.bom import Bom, BomItem
from rebrickable.bridge.models import (
    ColorMatch,
    MatchCandidate,
    PartMatch,
    TranslatedBomRow,
    TranslationReport,
)
from rebrickable.catalog.models import BomRow, Color, Part
from rebrickable.cli import main
from rebrickable.exports import (
    catalog_bom_to_csv,
    escape_csv_formula,
    to_json,
    translation_to_csv,
)
from rebrickable.types import (
    ColorRef,
    MappingSource,
    MappingStatus,
    PartRef,
    freeze_json,
)


def test_json_is_versioned_and_deterministic() -> None:
    rendered = to_json({"b": 2, "a": 1}, schema="test")
    assert rendered == to_json({"a": 1, "b": 2}, schema="test")
    payload = json.loads(rendered)
    assert payload == {"schema": "test", "schema_version": 1, "data": {"a": 1, "b": 2}}


def test_json_serializes_frozen_mappings_and_filters_secrets() -> None:
    frozen = freeze_json({"outer": {"inner": 1}, "api_key": "secret-value"})
    payload = json.loads(to_json(frozen, schema="test"))
    assert payload["data"] == {"outer": {"inner": 1}}
    assert "secret-value" not in to_json(frozen)


def _injection_report() -> TranslationReport:
    part = PartMatch(
        "=cmd|' /C calc'!A0",
        None,
        MappingStatus.UNRESOLVED,
        MappingSource.NONE,
        0.0,
        (MatchCandidate("+alt", None, "candidate", 0.5),),
        "-note",
        "snap",
    )
    color = ColorMatch(
        4, 4, MappingStatus.RESOLVED, MappingSource.RGB, 0.9, (), "ok", "snap"
    )
    row = TranslatedBomRow(
        part.source_identifier, 4, None, 4, 1, MappingStatus.UNRESOLVED, part, color
    )
    return TranslationReport((row,), 0, 0, 1, "snap")


def test_csv_formula_injection_is_escaped() -> None:
    assert escape_csv_formula("=1+1") == "'=1+1"
    assert escape_csv_formula("@cmd") == "'@cmd"
    assert escape_csv_formula("3001") == "3001"

    translated = translation_to_csv(_injection_report())
    assert "'=cmd|' /C calc'!A0" in translated
    assert "'+alt" in translated
    assert "'-note; ok" in translated
    assert "\r\n=" not in translated

    catalog = catalog_bom_to_csv(
        (
            BomRow(
                Part("@3001", "Brick", 1, "Plastic"),
                Color(4, "Red", "C91A09", False, 1, 1, None, None),
                2,
                (),
            ),
        )
    )
    assert "'@3001" in catalog

    bom = Bom.normalize(
        [BomItem(PartRef("rebrickable", "=3001"), ColorRef("rebrickable", 4), 1)]
    )
    assert "'=3001" in bom.to_rebrickable_csv()
    benign = Bom.normalize(
        [BomItem(PartRef("rebrickable", "3001"), ColorRef("rebrickable", 4), 1)]
    )
    assert benign.to_rebrickable_csv() == "part_num,color_id,quantity\r\n3001,4,1\r\n"


def test_cli_url_and_api_spec(capsys) -> None:
    assert main(["url", "part", "3001"]) == 0
    assert capsys.readouterr().out == "https://rebrickable.com/parts/3001/\n"
    assert main(["api-spec"]) == 0
    assert json.loads(capsys.readouterr().out)["swagger"] == "2.0"


def test_api_spec_bytes_identical_between_stdout_and_output(
    capsys, tmp_path: Path
) -> None:
    assert main(["api-spec"]) == 0
    streamed = capsys.readouterr().out
    target = tmp_path / "spec.json"
    assert main(["api-spec", "--output", str(target)]) == 0
    assert target.read_bytes() == streamed.encode("utf-8")


def test_broken_pipe_exits_quietly(monkeypatch, capsys) -> None:
    async def burst(_args: object) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "_run", burst)
    assert main(["url", "part", "3001"]) == 0
    assert capsys.readouterr().err == ""
