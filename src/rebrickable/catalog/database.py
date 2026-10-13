"""Active snapshot resolution and asynchronous SQLite access."""

from __future__ import annotations

import json
import sqlite3
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite
from filelock import FileLock, Timeout

from rebrickable._workers import acquire_lock, run_in_thread, settle
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


@dataclass(frozen=True, slots=True, kw_only=True)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class _ActivePointer:
    snapshot_id: str | None
    data: dict[str, Any] = field(default_factory=dict)
    error: OSError | None = None


def _safe_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not value.endswith((".", " "))
        and not any(
            ord(character) < 32 or character in '<>:"/\\|?*' for character in value
        )
        and value.partition(".")[0].rstrip(" ").upper()
        not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "CONIN$",
            "CONOUT$",
            *(
                f"{prefix}{number}"
                for prefix in ("COM", "LPT")
                for number in "123456789¹²³"
            ),
        }
    )


def _snapshot_contained(paths: CatalogPaths, snapshot_id: str) -> bool:
    root = paths.snapshots_dir.resolve()
    snapshot = (paths.snapshots_dir / snapshot_id).resolve()
    return (
        snapshot.parent == root
        and paths.database_for(snapshot_id).resolve().parent == snapshot
        and paths.manifest_for(snapshot_id).resolve().parent == snapshot
    )


def _read_active_pointer(paths: CatalogPaths) -> _ActivePointer:
    try:
        data = json.loads(paths.active_pointer.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _ActivePointer(snapshot_id=None)
    except OSError as exc:
        return _ActivePointer(snapshot_id="", error=exc)
    except (UnicodeError, json.JSONDecodeError):
        return _ActivePointer(snapshot_id="")
    snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
    try:
        safe = (
            isinstance(snapshot_id, str)
            and _safe_snapshot_id(snapshot_id)
            and _snapshot_contained(paths, snapshot_id)
        )
    except OSError as exc:
        return _ActivePointer(snapshot_id="", error=exc)
    except RuntimeError:
        safe = False  # Python 3.12 reports symlink loops as RuntimeError.
    return _ActivePointer(
        snapshot_id=snapshot_id if safe else "",
        data=data if isinstance(data, dict) else {},
    )


def _read_pointer(paths: CatalogPaths) -> str | None:
    return _read_active_pointer(paths).snapshot_id


class _VerificationStatus(Enum):
    HEALTHY = auto()
    CORRUPT = auto()
    MISSING = auto()
    RECOVERY_REQUIRED = auto()
    OPERATIONAL_FAILURE = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class _DatabaseVerification:
    status: _VerificationStatus
    error: OSError | sqlite3.Error | None = None


def _verify_database(
    database: Path, *, recover_hot_journal: bool = False
) -> _DatabaseVerification:
    """Verify read-only, or roll back a confirmed hot journal under the lock."""
    try:
        if not stat.S_ISREG(database.stat().st_mode):
            return _DatabaseVerification(status=_VerificationStatus.MISSING)
        uri = _read_only_uri(database)
        if recover_hot_journal:
            uri = uri.removesuffix("mode=ro") + "mode=rw"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute("PRAGMA quick_check").fetchone()
        except BaseException:
            with suppress(OSError, sqlite3.Error):
                connection.close()
            raise
        else:
            connection.close()
        if row is None or row[0] != "ok":
            return _DatabaseVerification(
                status=_VerificationStatus.CORRUPT,
                error=sqlite3.DatabaseError("SQLite quick_check failed"),
            )
    except FileNotFoundError as exc:
        return _DatabaseVerification(status=_VerificationStatus.MISSING, error=exc)
    except (OSError, sqlite3.Error) as exc:
        code = getattr(exc, "sqlite_errorcode", 0)
        if code == sqlite3.SQLITE_READONLY_ROLLBACK:
            status = _VerificationStatus.RECOVERY_REQUIRED
        elif code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or code in {
            sqlite3.SQLITE_IOERR_CORRUPTFS,
            sqlite3.SQLITE_IOERR_DATA,
        }:
            status = _VerificationStatus.CORRUPT
        else:
            status = _VerificationStatus.OPERATIONAL_FAILURE
            # An opening race is absence only when a new stat confirms it.
            try:
                if not stat.S_ISREG(database.stat().st_mode):
                    status = _VerificationStatus.MISSING
            except FileNotFoundError:
                status = _VerificationStatus.MISSING
            except OSError:
                pass
        return _DatabaseVerification(status=status, error=exc)
    return _DatabaseVerification(status=_VerificationStatus.HEALTHY)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CatalogInspection:
    state: CatalogState
    verification: _DatabaseVerification | None = None
    error: OSError | sqlite3.Error | None = None


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

    With `verify=False` (the default) a structurally present snapshot is
    reported READY without opening SQLite; database corruption is detected by
    `open_catalog` (which always verifies) or an explicit `verify=True`.
    """
    return await _catalog_state(
        config, _read_active_pointer(CatalogPaths.from_config(config)), verify=verify
    )


async def _catalog_state(
    config: Config, pointer: _ActivePointer, *, verify: bool = False
) -> CatalogState:
    return (await _inspect_catalog(config, pointer, verify=verify)).state


async def _inspect_catalog(
    config: Config, pointer: _ActivePointer, *, verify: bool = False
) -> _CatalogInspection:
    paths = CatalogPaths.from_config(config)
    snapshot_id = pointer.snapshot_id
    if snapshot_id is None:
        return _CatalogInspection(
            state=CatalogState(
                status=CatalogStatus.MISSING,
                database_path=paths.configured_database,
                manifest_path=paths.active_pointer,
            )
        )
    if not snapshot_id:
        return _CatalogInspection(
            state=CatalogState(
                status=CatalogStatus.UNREADABLE,
                database_path=paths.configured_database,
                manifest_path=paths.active_pointer,
                diagnostics=("active snapshot pointer is invalid",),
            ),
            error=pointer.error,
        )
    database = paths.database_for(snapshot_id)
    manifest_path = paths.manifest_for(snapshot_id)

    def unavailable(
        message: str,
        *,
        status: CatalogStatus = CatalogStatus.UNREADABLE,
        error: OSError | sqlite3.Error | None = None,
        verification: _DatabaseVerification | None = None,
    ) -> _CatalogInspection:
        return _CatalogInspection(
            state=CatalogState(
                status=status,
                database_path=database,
                manifest_path=manifest_path,
                snapshot_id=snapshot_id,
                diagnostics=(message,),
            ),
            error=error,
            verification=verification,
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema_version = int(manifest["schema_version"])
        retrieved_at = datetime.fromisoformat(str(manifest["retrieved_at"]))
        files = tuple(_fingerprint(item) for item in manifest["files"])
    except FileNotFoundError:
        try:
            database.stat()
        except FileNotFoundError:
            status = CatalogStatus.UNREADABLE
        except OSError as exc:
            return unavailable(str(exc), error=exc)
        else:
            status = CatalogStatus.IMPORT_REQUIRED
        return unavailable("snapshot manifest is missing", status=status)
    except OSError as exc:
        return unavailable(f"unable to read manifest: {exc}", error=exc)
    except (KeyError, TypeError, ValueError) as exc:
        return unavailable(f"invalid manifest: {exc}")
    if schema_version != SCHEMA_VERSION:
        return _CatalogInspection(
            state=CatalogState(
                status=CatalogStatus.SCHEMA_MISMATCH,
                database_path=database,
                manifest_path=manifest_path,
                snapshot_id=snapshot_id,
                retrieved_at=retrieved_at,
                files=files,
                schema_version=schema_version,
            )
        )
    try:
        present = stat.S_ISREG(database.stat().st_mode)
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return unavailable(str(exc), error=exc)
    if not present:
        return unavailable(
            "snapshot database is missing",
            verification=_DatabaseVerification(
                status=_VerificationStatus.MISSING,
            ),
        )
    verification = await run_in_thread(_verify_database, database) if verify else None
    if (
        verification is not None
        and verification.status is not _VerificationStatus.HEALTHY
    ):
        return unavailable(
            str(verification.error or "snapshot database is missing"),
            verification=verification,
            error=verification.error
            if verification.status is _VerificationStatus.OPERATIONAL_FAILURE
            else None,
        )
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)
    return _CatalogInspection(
        state=CatalogState(
            status=CatalogStatus.READY,
            database_path=database,
            manifest_path=manifest_path,
            snapshot_id=snapshot_id,
            retrieved_at=retrieved_at,
            files=files,
            schema_version=schema_version,
        ),
        verification=verification,
    )


async def open_catalog(config: Config) -> tuple[aiosqlite.Connection, CatalogState]:
    """Open the active snapshot read-only under the promotion lock.

    Pointer resolution, verification, and connecting all hold the promotion
    lock so a concurrent refresh cannot prune the resolved snapshot; opens
    therefore serialize and raise `CatalogUnavailableError` after
    `Config.lock_timeout` seconds of contention.
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
            await acquire_lock(lock)
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
        connection = aiosqlite.connect(_read_only_uri(state.database_path), uri=True)
        try:
            connection = await settle(connection)
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only = ON")
        except BaseException:
            await settle(connection.close())
            raise
        return connection, state
    finally:
        if lock is not None:
            lock.release()
