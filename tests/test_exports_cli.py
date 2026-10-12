from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO
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
        source_identifier="=cmd|' /C calc'!A0",
        target_identifier=None,
        status=MappingStatus.UNRESOLVED,
        source=MappingSource.NONE,
        confidence=0.0,
        candidates=(
            MatchCandidate(
                identifier="+alt", name=None, reason="candidate", confidence=0.5
            ),
        ),
        explanation="-note",
        snapshot_id="snap",
    )
    color = ColorMatch(
        source_identifier=4,
        target_identifier=4,
        status=MappingStatus.RESOLVED,
        source=MappingSource.RGB,
        confidence=0.9,
        candidates=(),
        explanation="ok",
        snapshot_id="snap",
    )
    row = TranslatedBomRow(
        ldraw_part_num=part.source_identifier,
        ldraw_color_code=4,
        rebrickable_part_num=None,
        rebrickable_color_id=4,
        quantity=1,
        status=MappingStatus.UNRESOLVED,
        part_match=part,
        color_match=color,
    )
    return TranslationReport(
        rows=(row,),
        resolved_count=0,
        ambiguous_count=0,
        unresolved_count=1,
        snapshot_id="snap",
    )


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
                part=Part(
                    part_num="@3001", name="Brick", category_id=1, material="Plastic"
                ),
                color=Color(
                    id=4,
                    name="Red",
                    rgb="C91A09",
                    is_trans=False,
                    num_parts=1,
                    num_sets=1,
                    year_from=None,
                    year_to=None,
                ),
                quantity=2,
                provenance=(),
            ),
        )
    )
    assert "'@3001" in catalog

    bom = Bom.normalize(
        [
            BomItem(
                part=PartRef(system="rebrickable", value="=3001"),
                color=ColorRef(system="rebrickable", value=4),
                quantity=1,
            )
        ]
    )
    assert "'=3001" in bom.to_rebrickable_csv()
    benign = Bom.normalize(
        [
            BomItem(
                part=PartRef(system="rebrickable", value="3001"),
                color=ColorRef(system="rebrickable", value=4),
                quantity=1,
            )
        ]
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
def broken_pipe_io(monkeypatch, capsys) -> tuple[Mock, Mock, Mock, Mock]:
    capsys.readouterr()

    async def burst(_args: object) -> int:
        cli._write_stdout("output")
        return 0

    stdout = Mock(spec=io.TextIOWrapper)
    stdout.buffer = Mock(spec=io.BufferedWriter)
    stdout.buffer.raw = Mock(spec=io.FileIO)
    stdout.closed = False
    stdout.write.side_effect = BrokenPipeError
    stdout.fileno.return_value = 1
    open_descriptor = Mock(return_value=9)
    duplicate = Mock()
    close_descriptor = Mock()
    monkeypatch.setattr(cli, "_run", burst)
    monkeypatch.setattr(cli, "sys", _CliStreams(stdout=stdout, __stdout__=stdout))
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
    stdout.write.assert_called_once_with("output")
    if failure is not None and failure.startswith("stdout_"):
        open_descriptor.assert_not_called()
        duplicate.assert_not_called()
        close_descriptor.assert_not_called()
        stdout.buffer.raw.close.assert_called_once_with()
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


@pytest.mark.usefixtures("broken_pipe_io")
def test_broken_pipe_fixture_captures_diagnostics(capsys) -> None:
    cli._print_stderr("injected diagnostic")
    assert capsys.readouterr() == ("", "injected diagnostic\n")


class _CliStreams(SimpleNamespace):
    @property
    def stderr(self) -> TextIO | None:
        return sys.stderr


def test_repeated_broken_pipes_release_temporary_descriptors() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """\
import os
import sys
from types import SimpleNamespace
from rebrickable import cli
opened = []
def allocate(path, flags):
    descriptor = os.open(path, flags)
    opened.append(descriptor)
    return descriptor
cli.os = SimpleNamespace(
    devnull=os.devnull, O_WRONLY=os.O_WRONLY,
    open=allocate, dup2=os.dup2, close=os.close,
)
for _ in range(10):
    cli._silence_stream(sys.stdout)
    sys.stdout.write('discarded output')
    sys.stdout.flush()
    try:
        os.fstat(opened[-1])
    except OSError:
        pass
    else:
        raise AssertionError('temporary descriptor leaked')
assert len(opened) == 10
""",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == b""
