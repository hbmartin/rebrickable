from __future__ import annotations

import json

from rebrickable.cli import main
from rebrickable.exports import to_json


def test_json_is_versioned_and_deterministic() -> None:
    payload = json.loads(to_json({"b": 2, "a": 1}, schema="test"))
    assert payload == {"schema": "test", "schema_version": 1, "data": {"a": 1, "b": 2}}


def test_cli_url_and_api_spec(capsys) -> None:
    assert main(["url", "part", "3001"]) == 0
    assert capsys.readouterr().out == "https://rebrickable.com/parts/3001/\n"
    assert main(["api-spec"]) == 0
    assert json.loads(capsys.readouterr().out)["swagger"] == "2.0"
