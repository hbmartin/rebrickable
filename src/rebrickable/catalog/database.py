"""Active snapshot resolution and asynchronous SQLite access."""

from __future__ import annotations

import errno
import json
import os
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
from rebrickable.catalog.schema import _REQUIRED_COLUMNS, SCHEMA_VERSION
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

    @property
    def activation_id(self) -> str | None:
        value = self.data.get("snapshot_id", self.snapshot_id)
        return value if isinstance(value, str) and _safe_snapshot_id(value) else None


_RESERVED_NAMES = frozenset(
    {
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
_STRUCTURAL_ERRNOS = frozenset(
    {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.EISDIR,
        errno.ENAMETOOLONG,
        errno.ELOOP,
    }
)
_SQLITE_IOERR_DATA = getattr(sqlite3, "SQLITE_IOERR_DATA", 8202)
_SQLITE_IOERR_CORRUPTFS = getattr(sqlite3, "SQLITE_IOERR_CORRUPTFS", 8458)
_CORRUPTION_CODES = frozenset({_SQLITE_IOERR_DATA, _SQLITE_IOERR_CORRUPTFS})


def _structural_path_error(error: OSError) -> bool:
    return error.errno in _STRUCTURAL_ERRNOS


def _resolve_catalog_path(path: Path) -> Path:
    try:
        try:
            return path.resolve(strict=True)
        except FileNotFoundError:
            return path.resolve()
    except RuntimeError as exc:
        # Normalize Python 3.12's loop exception to the later Python behavior.
        raise OSError(
            errno.ELOOP, "snapshot contains a symlink loop", str(path)
        ) from exc


def _safe_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not value.endswith((".", " "))
        and not any(
            ord(character) < 32 or character in '<>:"/\\|?*' for character in value
        )
        and value.partition(".")[0].rstrip(" ").upper() not in _RESERVED_NAMES
    )


def _contained_snapshot(paths: CatalogPaths, snapshot_id: str) -> Path | None:
    root = _resolve_catalog_path(paths.snapshots_dir)
    snapshot = _resolve_catalog_path(paths.snapshots_dir / snapshot_id)
    return (
        snapshot
        if (
            snapshot.parent == root
            and _resolve_catalog_path(paths.database_for(snapshot_id)).parent
            == snapshot
        )
        else None
    )


def _database_contained(paths: CatalogPaths, snapshot_id: str) -> bool:
    return _contained_snapshot(paths, snapshot_id) is not None


def _snapshot_contained(paths: CatalogPaths, snapshot_id: str) -> bool:
    snapshot = _contained_snapshot(paths, snapshot_id)
    return snapshot is not None and (
        _resolve_catalog_path(paths.manifest_for(snapshot_id)).parent == snapshot
    )


def _read_active_pointer(paths: CatalogPaths) -> _ActivePointer:
    try:
        descriptor = os.open(
            paths.active_pointer,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return _ActivePointer(snapshot_id="")
            handle = os.fdopen(descriptor, "r", encoding="utf-8")
            descriptor = -1  # Ownership transfers to the text stream.
            with handle:
                data = json.load(handle)
        finally:
            if descriptor != -1:
                os.close(descriptor)
    except FileNotFoundError:
        return _ActivePointer(snapshot_id=None)
    except OSError as exc:
        return _ActivePointer(
            snapshot_id="", error=None if _structural_path_error(exc) else exc
        )
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
        return _ActivePointer(
            snapshot_id="",
            data=data if isinstance(data, dict) else {},
            error=None if _structural_path_error(exc) else exc,
        )
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
    INCOMPATIBLE = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class _DatabaseVerification:
    status: _VerificationStatus
    error: OSError | sqlite3.Error | None = None
    snapshot_id: str | None = None


def _sqlite_corruption(code: int) -> bool:
    return (
        code & 0xFF in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
        or code in _CORRUPTION_CODES
    )


def _copy_source_failure(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", 0)
    return (
        _sqlite_corruption(code)
        or code & 0xFF
        in {
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_ERROR,
        }
        or code
        in {
            sqlite3.SQLITE_READONLY_ROLLBACK,
            sqlite3.SQLITE_IOERR_READ,
            sqlite3.SQLITE_IOERR_SHORT_READ,
            sqlite3.SQLITE_IOERR_FSTAT,
        }
    )


def _schema_structural_error(error: sqlite3.Error) -> bool:
    """Recognize broken dependencies without assuming unknown SQLite errors are damage."""
    message = str(error)
    return message.startswith(("no such table:", "no such column:")) or (
        message.startswith("view ") and message.endswith(" is circularly defined")
    )


def _catalog_problem(connection: sqlite3.Connection) -> _DatabaseVerification:
    def corrupt(message: str) -> _DatabaseVerification:
        return _DatabaseVerification(
            status=_VerificationStatus.CORRUPT, error=sqlite3.DatabaseError(message)
        )

    objects = dict(connection.execute("SELECT name, type FROM sqlite_master"))
    if objects.get("snapshot_meta") != "table":
        return corrupt("catalog is missing required table snapshot_meta")
    metadata = dict(connection.execute("SELECT key, value FROM snapshot_meta"))
    version = metadata.get("schema_version", "")
    if (
        not isinstance(version, str)
        or not version.isascii()
        or not version.isdecimal()
        or version.startswith("0")
    ):
        return corrupt("catalog metadata schema version is missing or inconsistent")
    snapshot_id = metadata.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return corrupt("catalog metadata snapshot identity is missing")
    if version != str(SCHEMA_VERSION):
        return _DatabaseVerification(
            status=_VerificationStatus.INCOMPATIBLE,
            error=sqlite3.DatabaseError(
                f"unsupported catalog schema version {version}"
            ),
            snapshot_id=snapshot_id,
        )
    for name, (kind, required) in _REQUIRED_COLUMNS.items():
        if objects.get(name) != kind:
            return corrupt(f"catalog is missing required {kind} {name}")
        # Identifiers come exclusively from the closed schema allowlist.
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info('{name}')")}
        if not required.issubset(columns):
            return corrupt(f"catalog has incomplete columns for {name}")
    return _DatabaseVerification(
        status=_VerificationStatus.HEALTHY, snapshot_id=snapshot_id
    )


def _verify_database(
    database: Path, *, recover_hot_journal: bool = False
) -> _DatabaseVerification:
    """Verify read-only, or roll back a confirmed hot journal under the lock."""
    probing_schema = False
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
            result = _DatabaseVerification(
                status=_VerificationStatus.CORRUPT,
                error=sqlite3.DatabaseError("SQLite quick_check failed"),
            )
            if row is not None and row[0] == "ok":
                probing_schema = True
                result = _catalog_problem(connection)
                probing_schema = False
        except BaseException:
            with suppress(BaseException):
                connection.close()
            raise
        else:
            connection.close()
            return result
    except FileNotFoundError as exc:
        return _DatabaseVerification(status=_VerificationStatus.MISSING, error=exc)
    except (OSError, sqlite3.Error) as exc:
        code = getattr(exc, "sqlite_errorcode", 0)
        if code == sqlite3.SQLITE_READONLY_ROLLBACK:
            status = _VerificationStatus.RECOVERY_REQUIRED
        elif (
            (
                probing_schema
                and isinstance(exc, sqlite3.Error)
                and code & 0xFF == sqlite3.SQLITE_ERROR
                and _schema_structural_error(exc)
            )
            or _sqlite_corruption(code)
            or (isinstance(exc, OSError) and _structural_path_error(exc))
        ):
            status = _VerificationStatus.CORRUPT
        else:
            status = _VerificationStatus.OPERATIONAL_FAILURE
            # An opening race is absence only when a new stat confirms it.
            try:
                if not stat.S_ISREG(database.stat().st_mode):
                    status = _VerificationStatus.MISSING
            except FileNotFoundError:
                status = _VerificationStatus.MISSING
            except OSError as path_error:
                if _structural_path_error(path_error):
                    status = _VerificationStatus.CORRUPT
        return _DatabaseVerification(status=status, error=exc)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CatalogInspection:
    state: CatalogState
    verification: _DatabaseVerification | None = None
    error: OSError | sqlite3.Error | None = None
    cache_skip_reason: str | None = None


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
    return (
        await _inspect_catalog(
            config,
            _read_active_pointer(CatalogPaths.from_config(config)),
            verify=verify,
        )
    ).state


async def _inspect_catalog(
    config: Config,
    pointer: _ActivePointer,
    *,
    verify: bool = False,
    verification: _DatabaseVerification | None = None,
) -> _CatalogInspection:
    paths = CatalogPaths.from_config(config)
    snapshot_id = pointer.snapshot_id
    if snapshot_id is None:
        return _CatalogInspection(
            state=CatalogState(
                status=CatalogStatus.MISSING,
                database_path=paths.configured_database,
                manifest_path=paths.active_pointer,
            ),
            verification=verification,
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
            verification=verification,
        )
    database = paths.database_for(snapshot_id)
    manifest_path = paths.manifest_for(snapshot_id)

    def unavailable(
        message: str,
        *,
        status: CatalogStatus = CatalogStatus.UNREADABLE,
        error: OSError | sqlite3.Error | None = None,
        database_verification: _DatabaseVerification | None = None,
        cache_skip_reason: str | None = None,
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
            verification=database_verification or verification,
            cache_skip_reason=cache_skip_reason,
        )

    try:
        if not stat.S_ISREG(manifest_path.stat().st_mode):
            return unavailable("snapshot manifest is not a regular file")
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
            return unavailable(
                str(exc), error=None if _structural_path_error(exc) else exc
            )
        else:
            status = CatalogStatus.IMPORT_REQUIRED
        return unavailable("snapshot manifest is missing", status=status)
    except OSError as exc:
        return unavailable(
            f"unable to read manifest: {exc}",
            error=None if _structural_path_error(exc) else exc,
        )
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
            ),
            verification=verification,
        )
    try:
        present = stat.S_ISREG(database.stat().st_mode)
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return unavailable(str(exc), error=None if _structural_path_error(exc) else exc)
    if not present:
        return unavailable(
            "snapshot database is missing",
            database_verification=_DatabaseVerification(
                status=_VerificationStatus.MISSING,
            ),
        )
    if verify:
        verification = await run_in_thread(_verify_database, database)
    if (
        verification is not None
        and verification.status is _VerificationStatus.HEALTHY
        and "snapshot_id" in manifest
        and manifest["snapshot_id"] != verification.snapshot_id
    ):
        return unavailable(
            "database and manifest snapshot identities differ",
            cache_skip_reason="previous catalog identity disagrees with its manifest",
        )
    if (
        verification is not None
        and verification.status is not _VerificationStatus.HEALTHY
    ):
        return unavailable(
            "hot rollback journal requires recovery; run `rebrickable refresh` to attempt recovery"
            if verification.status is _VerificationStatus.RECOVERY_REQUIRED
            else str(verification.error or "snapshot database is missing"),
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
            detail = "; ".join(state.diagnostics)
            raise CatalogUnreadableError(f"active catalog is unreadable: {detail}")
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
