"""Safe API response decoding."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from rebrickable.errors import ApiDecodeError

T = TypeVar("T", bound=BaseModel)


def decode_model(
    model: type[T],
    payload: Any,
    *,
    operation_id: str,
    path_template: str,
) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ApiDecodeError(
            operation_id=operation_id,
            path_template=path_template,
            detail="response did not match the recorded contract",
        ) from exc
