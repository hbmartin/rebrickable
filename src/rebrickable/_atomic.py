"""Small durable-filesystem primitives shared by persistence modules."""

from __future__ import annotations

import os
from pathlib import Path

_replace = os.replace


def durable_replace(source: Path, target: Path) -> None:
    """Atomically replace ``target`` and durably record its directory entry."""
    _replace(source, target)
    if os.name == "nt":
        return
    descriptor = os.open(
        target.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
