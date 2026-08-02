"""Framework-neutral progress contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProgressStage(StrEnum):
    DOWNLOAD = "download"
    VALIDATE = "validate"
    IMPORT = "import"
    INDEX = "index"
    PROMOTE = "promote"
    DONE = "done"


class ProgressUnit(StrEnum):
    BYTES = "bytes"
    ROWS = "rows"
    FILES = "files"
    STEPS = "steps"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: ProgressStage
    dataset: str | None = None
    current: int | None = None
    total: int | None = None
    unit: ProgressUnit | None = None
    message: str = ""
    path: Path | None = None


ProgressCallback = Callable[[ProgressEvent], None]
