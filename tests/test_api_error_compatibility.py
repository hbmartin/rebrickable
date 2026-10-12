"""Compatibility with trusted exception pickles from earlier implementations."""

from __future__ import annotations

import base64
import json
import pickle
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from rebrickable import (
    ApiAuthenticationError,
    ApiDecodeError,
    ApiError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiServerError,
    ApiThrottledError,
)

_ERROR_TYPES = (
    ApiError,
    ApiAuthenticationError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiThrottledError,
    ApiServerError,
)
_VALUES = (429, "/parts/", "parts", "slow down", "request", 2.0)
# Protocol-4 fixtures were generated from errors.py at 751235e^ and 751235e.
_HISTORICAL_PICKLES = json.loads(
    Path(__file__).with_name("fixtures").joinpath("api_error_pickles.json").read_text()
)


@pytest.mark.parametrize("pickle_format", ["released", "garuff"])
@pytest.mark.parametrize("error_type", _ERROR_TYPES)
def test_historical_api_errors_load(
    pickle_format: str, error_type: type[ApiError]
) -> None:
    payload = base64.b64decode(_HISTORICAL_PICKLES[pickle_format][error_type.__name__])
    restored = pickle.loads(payload)  # noqa: S301 - owned historical fixtures
    assert type(restored) is error_type
    assert restored == error_type(*_VALUES)
    assert restored.args == _VALUES
    assert restored.request_id == "request"
    assert restored.retry_after == 2.0


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
@pytest.mark.parametrize("error_type", _ERROR_TYPES)
def test_current_api_errors_round_trip(
    error_type: type[ApiError], protocol: int
) -> None:
    error = error_type(*_VALUES)
    restored = pickle.loads(pickle.dumps(error, protocol))  # noqa: S301 - owned value
    assert type(restored) is error_type
    assert restored == error
    assert restored.args == error.args


@pytest.mark.parametrize("error_type", _ERROR_TYPES)
def test_api_error_defaults_round_trip(error_type: type[ApiError]) -> None:
    error = error_type(*_VALUES[:4])
    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 - owned value
    assert type(restored) is error_type
    assert restored.args == (*_VALUES[:4], None, None)
    assert restored.request_id is None
    assert restored.retry_after is None


@pytest.mark.parametrize("pickle_format", ["released", "garuff"])
def test_historical_decode_error_discards_payload(pickle_format: str) -> None:
    payload = base64.b64decode(_HISTORICAL_PICKLES[pickle_format]["ApiDecodeError"])
    restored = pickle.loads(payload)  # noqa: S301 - owned historical fixture
    assert type(restored) is ApiDecodeError
    assert restored.args == (None, "/parts/", "parts", "not JSON", None, None)
    assert not hasattr(restored, "payload")
    assert b"discarded-fixture-payload" not in payload


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_decode_error_round_trip_discards_payload(protocol: int) -> None:
    error = ApiDecodeError(
        path_template="/parts/",
        operation_id="parts",
        detail="not JSON",
        payload={"sensitive": "discarded-fixture-payload"},
    )
    payload = pickle.dumps(error, protocol)
    restored = pickle.loads(payload)  # noqa: S301 - owned value
    assert type(restored) is ApiDecodeError
    assert restored == error
    assert restored.args == error.args
    assert not hasattr(restored, "payload")
    assert b"discarded-fixture-payload" not in payload


@pytest.mark.parametrize("error_type", [*_ERROR_TYPES, ApiDecodeError])
@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_exception_notes_preserve_fields_and_pickle(error_type, protocol) -> None:
    error = (
        ApiDecodeError(
            path_template="/parts/",
            operation_id="parts",
            detail="invalid",
            payload="private payload",
        )
        if error_type is ApiDecodeError
        else error_type(*_VALUES)
    )
    original_hash = hash(error)
    error.add_note("first context")
    error.add_note("second context")
    assert error.__notes__ == ["first context", "second context"]
    with pytest.raises(FrozenInstanceError):
        error.detail = "changed"
    assert hash(error) == original_hash
    payload = pickle.dumps(error, protocol)
    restored = pickle.loads(payload)  # noqa: S301 - owned value
    assert restored == error
    assert restored.args == error.args
    assert restored.__notes__ == error.__notes__
    assert b"private payload" not in payload


@pytest.mark.parametrize("note", [None, 1, b"bytes", ["list"]])
def test_exception_notes_match_builtin_validation(note) -> None:
    builtin = Exception("failure")
    structured = ApiError(*_VALUES)
    with pytest.raises(TypeError) as expected:
        builtin.add_note(note)
    with pytest.raises(TypeError) as actual:
        structured.add_note(note)
    assert str(actual.value) == str(expected.value)
    assert not hasattr(structured, "__notes__")


def test_exception_notes_reject_invalid_existing_container() -> None:
    error = ApiError(*_VALUES)
    object.__setattr__(error, "__notes__", "invalid")
    with pytest.raises(TypeError, match="__notes__"):
        error.add_note("context")


def test_exception_state_restores_metadata_without_changing_fields() -> None:
    error = ApiError(*_VALUES)
    error.__setstate__(None)
    assert not hasattr(error, "__notes__")
    error.__setstate__({"__notes__": ["restored context"]})
    assert error.__notes__ == ["restored context"]
    assert error == ApiError(*_VALUES)
    assert error.args == _VALUES
