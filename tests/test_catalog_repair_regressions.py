from __future__ import annotations

import errno
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from rebrickable import CatalogStatus
from rebrickable import refresh as refresh_module
from rebrickable.catalog import database
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.errors import CatalogImportError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


@pytest.mark.parametrize("target", ["manifest", "database", "snapshot"])
@pytest.mark.parametrize("damage", ["directory", "loop"])
async def test_structural_active_damage_allows_replacement(
    catalog_config, monkeypatch, target, damage
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    snapshot = paths.snapshots_dir / "fixture-snapshot"
    path = {
        "manifest": paths.manifest_for(snapshot.name),
        "database": paths.database_for(snapshot.name),
        "snapshot": snapshot,
    }[target]
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    if damage == "loop":
        path.symlink_to(path.name)
    elif target == "snapshot":
        path.write_bytes(b"not a directory")
    else:
        path.mkdir()
    monkeypatch.setattr(refresh_module, "_download_one", downloader(sources))
    report = await refresh_module.refresh_catalog(catalog_config)
    state = await catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.READY
    assert state.snapshot_id == report.snapshot_id
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize(
    "error_number", [errno.ENOTDIR, errno.EISDIR, errno.ENAMETOOLONG, errno.ELOOP]
)
def test_structural_errors_are_replacement_eligible(
    catalog_config, monkeypatch, error_number
) -> None:
    error = OSError(error_number, "damaged snapshot path")
    monkeypatch.setattr(database.sqlite3, "connect", Mock(side_effect=error))
    result = database._verify_database(
        CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    )
    assert result.status is database._VerificationStatus.CORRUPT
    assert result.error is error


@pytest.mark.parametrize(
    "damage", ["empty", "table", "view", "column", "version", "identity"]
)
async def test_schema_incomplete_catalogs_are_replaced(
    catalog_config, monkeypatch, damage
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = paths.database_for("fixture-snapshot")
    sources = stable_sources(catalog_config)
    if damage == "empty":
        previous.write_bytes(b"")
    else:
        with closing(sqlite3.connect(previous)) as connection:
            if damage == "table":
                connection.execute("DROP TABLE api_crosswalk_cache")
            elif damage == "view":
                connection.execute("DROP VIEW latest_inventories")
            elif damage == "column":
                connection.execute("ALTER TABLE colors RENAME COLUMN rgb TO bad_rgb")
            else:
                key = "schema_version" if damage == "version" else "snapshot_id"
                connection.execute(
                    "UPDATE snapshot_meta SET value='wrong' WHERE key=?", (key,)
                )
            connection.commit()
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.UNREADABLE
    monkeypatch.setattr(
        refresh_module, "_download_one", downloader(sources, unchanged=True)
    )
    await refresh_module.refresh_catalog(catalog_config)
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY


async def test_extra_schema_objects_preserve_legacy_compatibility(
    catalog_config,
) -> None:
    path = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE extra (value TEXT)")
        connection.execute("ALTER TABLE colors ADD COLUMN extra TEXT")
        connection.commit()
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY


async def test_uncertain_history_preserves_provenance_without_using_retention_slot(
    catalog_config, monkeypatch
) -> None:
    config = replace(catalog_config, snapshot_retention=2)
    paths = CatalogPaths.from_config(config)
    uncertain = paths.snapshots_dir / "old-loop"
    uncertain.mkdir()
    (uncertain / "manifest.json").symlink_to("manifest.json")
    generation = uuid.uuid4().hex
    refresh_module._atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": generation,
            "_activation_history": ["fixture-snapshot", uncertain.name],
        },
    )
    sources = stable_sources(config)
    monkeypatch.setattr(refresh_module, "_download_one", downloader(sources))
    first = await refresh_module.refresh_catalog(config)
    second = await refresh_module.refresh_catalog(config)
    assert uncertain.is_dir()
    assert (paths.snapshots_dir / first.snapshot_id).is_dir()
    assert (paths.snapshots_dir / second.snapshot_id).is_dir()
    assert not (paths.snapshots_dir / "fixture-snapshot").exists()
    assert (
        uncertain.name
        in json.loads(paths.active_pointer.read_text())["_activation_history"]
    )
    (uncertain / "manifest.json").unlink()
    await refresh_module.refresh_catalog(config)
    assert not uncertain.exists()


def test_optional_sqlite_constants_are_not_required_at_import() -> None:
    code = """
import sqlite3
for name in ('SQLITE_IOERR_DATA', 'SQLITE_IOERR_CORRUPTFS'):
    if hasattr(sqlite3, name):
        delattr(sqlite3, name)
from rebrickable.catalog.database import _sqlite_corruption, _copy_source_failure
for number in (8202, 8458):
    error = sqlite3.DatabaseError('corruption')
    error.sqlite_errorcode = number
    assert _sqlite_corruption(number)
    assert _copy_source_failure(error)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_operational_probe_is_wrapped_at_carryover_boundary(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    source = paths.database_for("fixture-snapshot")
    original = sqlite3.OperationalError("source cannot open")
    original.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    resource = PermissionError(errno.EACCES, "probe denied")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = original
    monkeypatch.setattr(
        refresh_module.sqlite3, "connect", Mock(return_value=connection)
    )
    monkeypatch.setattr(
        refresh_module,
        "_verify_database",
        Mock(
            return_value=database._DatabaseVerification(
                status=database._VerificationStatus.OPERATIONAL_FAILURE, error=resource
            )
        ),
    )
    with pytest.raises(CatalogImportError) as raised:
        refresh_module._carry_crosswalk(source, source)
    assert raised.value.__cause__ is resource
    assert resource.__cause__ is original
    connection.close.assert_called_once()


@pytest.mark.parametrize("repeats", [False, True])
def test_external_journal_recovery_allows_one_copy_retry(
    catalog_config, monkeypatch, repeats
) -> None:
    previous = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    error = sqlite3.OperationalError("rollback needed")
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [
        error,
        error if repeats else None,
        None,
        None,
        None,
    ]
    verify = Mock(
        return_value=database._DatabaseVerification(
            status=database._VerificationStatus.HEALTHY
        )
    )
    monkeypatch.setattr(refresh_module, "_verify_database", verify)
    monkeypatch.setattr(
        refresh_module,
        "_catalog_metadata",
        Mock(
            return_value=database._DatabaseVerification(
                status=database._VerificationStatus.HEALTHY
            )
        ),
    )
    if repeats:
        with pytest.raises(sqlite3.OperationalError):
            refresh_module._copy_crosswalk(connection, previous)
        assert verify.call_count == 2
    else:
        assert not refresh_module._copy_crosswalk(connection, previous)
        verify.assert_called_once_with(previous)
    connection.rollback.assert_called()


@pytest.mark.parametrize("number", [sqlite3.SQLITE_ERROR, 779, 8458, 1294])
def test_copy_schema_and_extended_failures_probe_source(
    catalog_config, monkeypatch, number
) -> None:
    error = sqlite3.DatabaseError("source invalid")
    error.sqlite_errorcode = number
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = error
    verify = Mock(
        return_value=database._DatabaseVerification(
            status=database._VerificationStatus.CORRUPT
        )
    )
    monkeypatch.setattr(refresh_module, "_verify_database", verify)
    assert refresh_module._copy_crosswalk(
        connection,
        CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot"),
    )
    verify.assert_called_once()


@pytest.mark.parametrize("initial_failure", [False, True])
def test_carryover_close_failure_is_structured_and_preserves_original(
    catalog_config, monkeypatch, initial_failure
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.close.side_effect = sqlite3.OperationalError("close failed")
    if initial_failure:
        connection.execute.side_effect = sqlite3.OperationalError(
            "original carryover failure"
        )
    monkeypatch.setattr(
        refresh_module.sqlite3, "connect", Mock(return_value=connection)
    )
    with pytest.raises(
        CatalogImportError,
        match="original carryover failure" if initial_failure else "close failed",
    ):
        refresh_module._carry_crosswalk(None, catalog_config.database_path)
    connection.close.assert_called_once()


def test_history_stat_failure_keeps_activation_provenance(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    monkeypatch.setattr(
        Path,
        "lstat",
        Mock(side_effect=PermissionError(errno.EACCES, "uncertain history")),
    )
    assert refresh_module._history_present(paths, "old-snapshot")
