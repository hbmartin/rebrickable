from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rebrickable import cli
from rebrickable.api.models import ApiPart, ApiUserToken
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
    to_csv,
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
    assert (
        json.loads(to_json(ApiPart(part_num="3001", name="Brick")))["data"]["part_num"]
        == "3001"
    )
    assert "secret" not in to_json(ApiUserToken(user_token="secret"))


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
    assert to_csv(({"b": 2, "a": "=1+1"},)) == "a,b\r\n'=1+1,2\r\n"


def test_to_csv_none_missing_and_nested_cells() -> None:
    assert to_csv(({"a": None, "b": 1}, {"b": 2})) == "a,b\r\n,1\r\n,2\r\n"
    assert to_csv(({"data": {"k": [1]}},)) == 'data\r\n"{""k"": [1]}"\r\n'
    assert to_csv(({"v": [1, 2]},)) == 'v\r\n"[1, 2]"\r\n'


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


@pytest.fixture
def broken_pipe_io(monkeypatch) -> tuple[Mock, Mock, Mock, Mock]:
    async def burst(_args: object) -> int:
        raise BrokenPipeError

    stdout = Mock()
    stdout.fileno.return_value = 1
    open_descriptor = Mock(return_value=9)
    duplicate = Mock()
    close_descriptor = Mock()
    monkeypatch.setattr(cli, "_run", burst)
    monkeypatch.setattr(
        cli, "sys", SimpleNamespace(stdout=stdout, stderr=cli.sys.stderr)
    )
    monkeypatch.setattr(
        cli,
        "os",
        SimpleNamespace(
            devnull=os.devnull,
            O_WRONLY=os.O_WRONLY,
            open=open_descriptor,
            dup2=duplicate,
            close=close_descriptor,
        ),
    )
    return stdout, open_descriptor, duplicate, close_descriptor


@pytest.mark.parametrize(
    ("failure", "devnull"),
    [
        pytest.param(None, 9, id="success"),
        pytest.param("stdout_os_error", 9, id="stdout-os-error"),
        pytest.param("stdout_closed", 9, id="closed-stdout"),
        pytest.param("stdout_unsupported", 9, id="unsupported-stdout"),
        pytest.param("open", 9, id="open-fails"),
        pytest.param("duplicate", 9, id="duplication-fails"),
        pytest.param("close", 9, id="close-fails"),
        pytest.param(None, 1, id="same-descriptor"),
    ],
)
def test_broken_pipe_exits_quietly(
    broken_pipe_io: tuple[Mock, Mock, Mock, Mock],
    capsys,
    failure: str | None,
    devnull: int,
) -> None:
    stdout, open_descriptor, duplicate, close_descriptor = broken_pipe_io
    stdout.fileno.side_effect = {
        "stdout_os_error": OSError,
        "stdout_closed": ValueError,
        "stdout_unsupported": io.UnsupportedOperation,
    }.get(failure)
    open_descriptor.return_value = devnull
    open_descriptor.side_effect = OSError if failure == "open" else None
    duplicate.side_effect = OSError if failure == "duplicate" else None
    close_descriptor.side_effect = OSError if failure == "close" else None

    assert main(["url", "part", "3001"]) == 0
    assert capsys.readouterr() == ("", "")
    stdout.fileno.assert_called_once_with()
    stdout.write.assert_not_called()
    if failure is not None and failure.startswith("stdout_"):
        open_descriptor.assert_not_called()
        duplicate.assert_not_called()
        close_descriptor.assert_not_called()
    else:
        open_descriptor.assert_called_once_with(os.devnull, os.O_WRONLY)
        if failure == "open":
            duplicate.assert_not_called()
            close_descriptor.assert_not_called()
        else:
            duplicate.assert_called_once_with(devnull, 1)
            if devnull == 1:
                close_descriptor.assert_not_called()
            else:
                close_descriptor.assert_called_once_with(devnull)


def test_repeated_broken_pipes_release_temporary_descriptors(
    broken_pipe_io: tuple[Mock, Mock, Mock, Mock], capsys
) -> None:
    _, open_descriptor, duplicate, close_descriptor = broken_pipe_io
    descriptors = iter(range(10, 20))
    outstanding: set[int] = set()

    def allocate(_path: str, _flags: int) -> int:
        descriptor = next(descriptors)
        outstanding.add(descriptor)
        return descriptor

    def release(descriptor: int) -> None:
        outstanding.remove(descriptor)

    open_descriptor.side_effect = allocate
    close_descriptor.side_effect = release
    for _ in range(10):
        assert main(["url", "part", "3001"]) == 0
        assert not outstanding
    assert open_descriptor.call_count == 10
    assert duplicate.call_count == 10
    assert close_descriptor.call_count == 10
    assert capsys.readouterr() == ("", "")
