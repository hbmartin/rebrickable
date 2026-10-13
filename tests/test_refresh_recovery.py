from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from rebrickable import CatalogState, CatalogStatus
from rebrickable import refresh as module
from rebrickable.catalog.database import (
    CatalogPaths,
    _DatabaseVerification,
    _read_active_pointer,
    _VerificationStatus,
    _verify_database,
    catalog_state,
)
from rebrickable.errors import CatalogImportError
from tests.test_refresh_state import downloader, snapshot_sources


def stable_sources(config):
    target = config.cache_path / "sources"
    target.mkdir(parents=True)
    return {
        name: Path(shutil.copy2(path, target / path.name))
        for name, path in snapshot_sources(config).items()
    }


@pytest.mark.parametrize("retain", [1, 2, 3])
@pytest.mark.parametrize(
    "damage",
    ["corrupt", "manifest-missing", "manifest-invalid", "schema", "database-missing"],
)
async def test_nonready_legacy_activation_ages_out(
    catalog_config, monkeypatch, retain, damage
) -> None:
    config = replace(catalog_config, snapshot_retention=retain)
    paths = CatalogPaths.from_config(config)
    sources = stable_sources(config)
    legacy = paths.snapshots_dir / "fixture-snapshot"
    manifest = paths.manifest_for(legacy.name)
    if damage == "corrupt":
        paths.database_for(legacy.name).write_bytes(b"not a SQLite database")
    elif damage == "manifest-missing":
        manifest.unlink()
    elif damage == "manifest-invalid":
        manifest.write_text("invalid JSON")
    elif damage == "schema":
        payload = json.loads(manifest.read_text())
        payload["schema_version"] = 2
        manifest.write_text(json.dumps(payload))
    else:
        paths.database_for(legacy.name).unlink()
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    for index in range(retain):
        await module.refresh_catalog(config)
        assert legacy.exists() is (index < retain - 1)
        assert (await catalog_state(config, verify=True)).status is CatalogStatus.READY


async def test_corruption_during_refresh_recovers_and_reports_skipped_cache(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original = module._inspect_catalog
    sources = stable_sources(catalog_config)
    events = []

    async def corrupt_before_promotion(
        config, pointer, *, verify=False
    ) -> CatalogState:
        if not verify:
            paths.database_for("fixture-snapshot").write_bytes(
                b"corrupted after verification"
            )
        return await original(config, pointer, verify=verify)

    monkeypatch.setattr(module, "_inspect_catalog", corrupt_before_promotion)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    assert any("skipped cached API mappings" in event.message for event in events)
    assert (
        await catalog_state(catalog_config, verify=True)
    ).snapshot_id == report.snapshot_id
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT source_id FROM user_mapping_overrides WHERE entity_kind='part'"
        ).fetchone() == ("3001",)
        assert connection.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] > 0
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))


def test_copy_failure_rolls_back_before_skipping_unreadable_source(
    catalog_config, tmp_path, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = paths.database_for("fixture-snapshot")
    candidate = tmp_path / "candidate.sqlite"
    shutil.copy2(previous, candidate)
    connection = sqlite3.connect(candidate)

    class PartialCopy:
        def execute(self, sql, parameters=()) -> sqlite3.Cursor:
            if sql.startswith("INSERT OR IGNORE INTO api_crosswalk_cache"):
                connection.execute(
                    "INSERT INTO api_crosswalk_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("part", "ldraw", "partial", "3001", "op", "date", "hash"),
                )
                error = sqlite3.DatabaseError("source corruption")
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                raise error
            return connection.execute(sql, parameters)

        def rollback(self) -> None:
            connection.rollback()

    monkeypatch.setattr(
        module,
        "_verify_database",
        lambda _path: _DatabaseVerification(status=_VerificationStatus.CORRUPT),
    )
    try:
        assert module._copy_crosswalk(PartialCopy(), previous)
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA database_list").fetchall() == [
            (0, "main", str(candidate))
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR_WRITE, sqlite3.SQLITE_IOERR_FSYNC],
)
async def test_candidate_storage_failure_stays_fatal(
    catalog_config, monkeypatch, code
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_bytes()
    original_connect = sqlite3.connect
    sources = stable_sources(catalog_config)
    verification = Mock()

    class WriteFailure:
        def __init__(self, connection) -> None:
            self.connection = connection

        def execute(self, sql, parameters=()) -> sqlite3.Cursor:
            if sql.startswith("INSERT OR IGNORE INTO api_crosswalk_cache"):
                error = sqlite3.OperationalError("candidate storage failed")
                error.sqlite_errorcode = code
                raise error
            return self.connection.execute(sql, parameters)

        def rollback(self) -> None:
            self.connection.rollback()

        def close(self) -> None:
            self.connection.close()

    def connect(
        database, *args: Any, **kwargs: Any
    ) -> sqlite3.Connection | WriteFailure:
        connection = original_connect(database, *args, **kwargs)
        return (
            WriteFailure(connection)
            if kwargs.get("uri") and isinstance(database, Path)
            else connection
        )

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    monkeypatch.setattr(module, "_verify_database", verification)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    with pytest.raises(CatalogImportError, match="candidate storage failed"):
        await module.refresh_catalog(catalog_config)
    verification.assert_not_called()
    assert paths.active_pointer.read_bytes() == original_pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


async def test_promotion_reuses_one_parsed_pointer(catalog_config, monkeypatch) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    original_read = Path.read_text
    reads = []

    def read(path, *args: Any, **kwargs: Any) -> str:
        if path == paths.active_pointer:
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    await module.refresh_catalog(catalog_config)
    assert (
        len(reads) == 3
    )  # Initial verification, locked resolution, pruning safety check.


@pytest.mark.parametrize(
    "snapshot_id", ["../outside", "..", "C:outside", "a\\b", "a\0b", 17, None]
)
async def test_unsafe_pointer_is_not_activation_provenance(
    catalog_config, snapshot_id
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text(json.dumps({"snapshot_id": snapshot_id}))
    assert _read_active_pointer(paths).snapshot_id == ""
    assert (await catalog_state(catalog_config)).status is CatalogStatus.UNREADABLE


@pytest.mark.parametrize("failure", ["healthy-source", "rollback", "detach"])
def test_copy_recovery_preserves_original_failure(
    catalog_config, monkeypatch, failure
) -> None:
    previous = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    error = sqlite3.DatabaseError("original read failure")
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [
        None,
        error,
        sqlite3.OperationalError("detach failed") if failure == "detach" else None,
    ]
    if failure == "rollback":
        connection.rollback.side_effect = sqlite3.OperationalError("rollback failed")
    if failure != "healthy-source":
        monkeypatch.setattr(
            module,
            "_verify_database",
            lambda _path: _DatabaseVerification(status=_VerificationStatus.CORRUPT),
        )
    with pytest.raises(sqlite3.DatabaseError, match="original read failure") as raised:
        module._copy_crosswalk(connection, previous)
    assert raised.value is error


@pytest.mark.parametrize("result", [None, ("corrupt",)])
def test_unreadable_source_verification_closes_connection(
    catalog_config, monkeypatch, result
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.return_value.fetchone.return_value = result
    monkeypatch.setattr(module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    result = _verify_database(
        CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    )
    assert result.status is _VerificationStatus.CORRUPT
    connection.close.assert_called_once_with()
