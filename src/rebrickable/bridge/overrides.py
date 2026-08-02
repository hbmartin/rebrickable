"""Versioned YAML mapping override persistence and SQLite materialization."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import yaml

from rebrickable.errors import ConfigLoadError


def read_overrides(path: Path | None) -> tuple[dict[str, str], ...]:
    if path is None or not path.exists():
        return ()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"invalid mapping overrides: {exc}") from exc
    if payload is None:
        return ()
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ConfigLoadError("mapping overrides require version: 1")
    records: list[dict[str, str]] = []
    for kind in ("part", "color"):
        entries = payload.get(kind + "s", [])
        if not isinstance(entries, list):
            raise ConfigLoadError(f"mapping override {kind}s must be a list")
        for item in entries:
            if not isinstance(item, dict):
                raise ConfigLoadError("mapping override entries must be mappings")
            source_system = str(item.get("source_system", "ldraw"))
            target_system = str(item.get("target_system", "rebrickable"))
            source_id = str(item.get("source_id", ""))
            target_id = str(item.get("target_id", ""))
            if not source_id or not target_id:
                raise ConfigLoadError("mapping override identifiers must not be empty")
            records.append(
                {
                    "entity_kind": kind,
                    "source_system": source_system,
                    "source_id": source_id,
                    "target_system": target_system,
                    "target_id": target_id,
                    "reason": str(item.get("reason", "")),
                },
            )
    return tuple(records)


def materialize_overrides(path: Path | None, database: Path) -> None:
    records = read_overrides(path)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM user_mapping_overrides")
        connection.executemany(
            "INSERT INTO user_mapping_overrides VALUES (:entity_kind,:source_system,:source_id,:target_system,:target_id,:reason)",
            records,
        )
        connection.commit()
    finally:
        connection.close()


def write_overrides(path: Path, records: tuple[dict[str, str], ...]) -> None:
    payload: dict[str, Any] = {"version": 1, "parts": [], "colors": []}
    for record in records:
        payload[record["entity_kind"] + "s"].append(
            {key: value for key, value in record.items() if key != "entity_kind"},
        )
    for key in ("parts", "colors"):
        payload[key].sort(
            key=lambda item: (
                item["source_system"],
                item["source_id"],
                item["target_system"],
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
