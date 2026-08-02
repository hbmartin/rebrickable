"""Active snapshot resolution and asynchronous SQLite access."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite
from filelock import FileLock, Timeout

from rebrickable.catalog.schema import SCHEMA_VERSION
from rebrickable.config import Config
from rebrickable.errors import (
    CatalogSchemaError,
    CatalogUnavailableError,
    CatalogUnreadableError,
)
from rebrickable.types import (
    CatalogState,
    CatalogStatus,
    FileFingerprint,
)


@dataclass(frozen=True, slots=True)
class CatalogPaths:
    configured_database: Path
    snapshots_dir: Path
    active_pointer: Path
    lock_file: Path

    @classmethod
    def from_config(cls, config: Config) -> CatalogPaths:
        database = config.database_path.expanduser()
        return cls(
            configured_database=database,
            snapshots_dir=database.parent / f".{database.stem}-snapshots",
            active_pointer=database.parent / f".{database.stem}-active.json",
            lock_file=database.parent / f".{database.stem}.lock",
        )

    def database_for(self, snapshot_id: str) -> Path:
        return self.snapshots_dir / snapshot_id / self.configured_database.name

    def manifest_for(self, snapshot_id: str) -> Path:
        return self.snapshots_dir / snapshot_id / "manifest.json"


def _read_only_uri(database: Path) -> str:
    return "file:" + quote(str(database), safe="/:") + "?mode=ro"


def _read_pointer(paths: CatalogPaths) -> str | None:
    try:
        data = json.loads(paths.active_pointer.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) else ""


def _fingerprint(item: dict[str, Any]) -> FileFingerprint:
    return FileFingerprint(
        dataset=str(item["dataset"]),
        url=str(item["url"]),
        etag=item.get("etag"),
        last_modified=item.get("last_modified"),
        byte_length=int(item["byte_length"]),
        sha256=str(item["sha256"]),
        row_count=int(item["row_count"]),
        unknown_columns=tuple(item.get("unknown_columns", ())),
    )


async def catalog_state(config: Config, *, verify: bool = False) -> CatalogState:
    """Classify the active snapshot.

    With ``verify=False`` (the default) a structurally present snapshot is
    reported READY without opening SQLite; database corruption is detected by
    ``open_catalog`` (which always verifies) or an explicit ``verify=True``.
    """
    paths = CatalogPaths.from_config(config)
    snapshot_id = _read_pointer(paths)
    if snapshot_id is None:
        return CatalogState(
            CatalogStatus.MISSING,
            paths.configured_database,
            paths.active_pointer,
        )
    if not snapshot_id:
        return CatalogState(
            CatalogStatus.UNREADABLE,
            paths.configured_database,
            paths.active_pointer,
            diagnostics=("active snapshot pointer is invalid",),
        )
    database = paths.database_for(snapshot_id)
    manifest_path = paths.manifest_for(snapshot_id)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema_version = int(manifest["schema_version"])
        retrieved_at = datetime.fromisoformat(str(manifest["retrieved_at"]))
        files = tuple(_fingerprint(item) for item in manifest["files"])
    except FileNotFoundError:
        status = (
            CatalogStatus.IMPORT_REQUIRED
            if database.exists()
            else CatalogStatus.UNREADABLE
        )
        return CatalogState(
            status,
            database,
            manifest_path,
            snapshot_id=snapshot_id,
            diagnostics=("snapshot manifest is missing",),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return CatalogState(
            CatalogStatus.UNREADABLE,
            database,
            manifest_path,
            snapshot_id=snapshot_id,
            diagnostics=(f"invalid manifest: {exc}",),
        )
    if schema_version != SCHEMA_VERSION:
        return CatalogState(
            CatalogStatus.SCHEMA_MISMATCH,
            database,
            manifest_path,
            snapshot_id=snapshot_id,
            retrieved_at=retrieved_at,
            files=files,
            schema_version=schema_version,
        )
    if not database.is_file():
        return CatalogState(
            CatalogStatus.UNREADABLE,
            database,
            manifest_path,
            snapshot_id=snapshot_id,
            diagnostics=("snapshot database is missing",),
        )
    if verify:
        try:
            async with aiosqlite.connect(
                _read_only_uri(database), uri=True
            ) as connection:
                row = await (await connection.execute("PRAGMA quick_check")).fetchone()
                if row is None or row[0] != "ok":
                    raise OSError("SQLite quick_check failed")
        except (OSError, aiosqlite.Error) as exc:
            return CatalogState(
                CatalogStatus.UNREADABLE,
                database,
                manifest_path,
                snapshot_id=snapshot_id,
                diagnostics=(str(exc),),
            )
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)
    return CatalogState(
        CatalogStatus.READY,
        database,
        manifest_path,
        snapshot_id=snapshot_id,
        retrieved_at=retrieved_at,
        files=files,
        schema_version=schema_version,
    )


async def open_catalog(config: Config) -> tuple[aiosqlite.Connection, CatalogState]:
    """Open the active snapshot read-only under the promotion lock.

    Pointer resolution, verification, and connecting all hold the promotion
    lock so a concurrent refresh cannot prune the resolved snapshot; opens
    therefore serialize and raise ``CatalogUnavailableError`` after
    ``Config.lock_timeout`` seconds of contention.
    """
    paths = CatalogPaths.from_config(config)
    lock: FileLock | None = None
    if paths.lock_file.parent.is_dir():
        lock = FileLock(
            paths.lock_file,
            timeout=config.lock_timeout,
            thread_local=False,
        )
        try:
            await asyncio.to_thread(lock.acquire)
        except Timeout as exc:
            raise CatalogUnavailableError("catalog is busy; retry opening it") from exc
    try:
        state = await catalog_state(config, verify=True)
        if state.status is CatalogStatus.SCHEMA_MISMATCH:
            raise CatalogSchemaError("catalog schema does not match this release")
        if state.status is CatalogStatus.UNREADABLE:
            raise CatalogUnreadableError("active catalog is unreadable")
        if state.status is not CatalogStatus.READY:
            raise CatalogUnavailableError(
                "catalog is not ready; run `rebrickable refresh`",
            )
        connection = await aiosqlite.connect(
            _read_only_uri(state.database_path),
            uri=True,
        )
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only = ON")
        except BaseException:
            await connection.close()
            raise
        return connection, state
    finally:
        if lock is not None:
            lock.release()
