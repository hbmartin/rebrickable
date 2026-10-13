from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.catalog.database import (
    CatalogPaths,
    _VerificationStatus,
    _verify_database,
)
from rebrickable.errors import CatalogImportError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (sqlite3.SQLITE_CORRUPT_INDEX, _VerificationStatus.CORRUPT),
        (sqlite3.SQLITE_CORRUPT_VTAB, _VerificationStatus.CORRUPT),
        (sqlite3.SQLITE_NOTADB, _VerificationStatus.CORRUPT),
        (sqlite3.SQLITE_IOERR_CORRUPTFS, _VerificationStatus.CORRUPT),
        (sqlite3.SQLITE_IOERR_DATA, _VerificationStatus.CORRUPT),
        (sqlite3.SQLITE_READONLY_ROLLBACK, _VerificationStatus.RECOVERY_REQUIRED),
        (sqlite3.SQLITE_CANTOPEN_ISDIR, _VerificationStatus.OPERATIONAL_FAILURE),
        (sqlite3.SQLITE_CANTOPEN_FULLPATH, _VerificationStatus.OPERATIONAL_FAILURE),
        (sqlite3.SQLITE_BUSY, _VerificationStatus.OPERATIONAL_FAILURE),
        (sqlite3.SQLITE_IOERR_READ, _VerificationStatus.OPERATIONAL_FAILURE),
        (sqlite3.SQLITE_IOERR_WRITE, _VerificationStatus.OPERATIONAL_FAILURE),
    ],
)
def test_verification_retains_extended_error_classification(
    catalog_config, monkeypatch, code, expected
) -> None:
    error = sqlite3.DatabaseError("verification failed")
    error.sqlite_errorcode = code
    monkeypatch.setattr(database.sqlite3, "connect", Mock(side_effect=error))
    result = _verify_database(
        CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    )
    assert result.status is expected
    assert result.error is error


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_CORRUPT_INDEX,
        sqlite3.SQLITE_IOERR_CORRUPTFS,
        sqlite3.SQLITE_CANTOPEN_FULLPATH,
    ],
)
def test_copy_probes_extended_source_errors(catalog_config, monkeypatch, code) -> None:
    previous = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    error = sqlite3.DatabaseError("source failed")
    error.sqlite_errorcode = code
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [error]
    verify = Mock(
        return_value=database._DatabaseVerification(status=_VerificationStatus.CORRUPT)
    )
    monkeypatch.setattr(module, "_verify_database", verify)
    assert module._copy_crosswalk(connection, previous)
    verify.assert_called_once_with(previous)
    connection.rollback.assert_called_once()


@pytest.mark.parametrize("where", ["pointer", "manifest", "stat", "sqlite"])
@pytest.mark.parametrize(
    "error",
    [PermissionError(errno.EACCES, "denied"), OSError(errno.EMFILE, "open file limit")],
)
async def test_operational_access_failure_preserves_active_catalog(
    catalog_config, monkeypatch, where, error
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_bytes()
    original_database = paths.database_for("fixture-snapshot").read_bytes()
    sources = stable_sources(catalog_config)
    download = Mock(wraps=downloader(sources))
    monkeypatch.setattr(module, "_download_one", download)
    target = (
        paths.active_pointer
        if where == "pointer"
        else paths.manifest_for("fixture-snapshot")
    )
    original_read = Path.read_text
    original_open = os.open
    original_stat = Path.stat

    def read(path, *args: Any, **kwargs: Any) -> str:
        if path == target:
            raise error
        return original_read(path, *args, **kwargs)

    def stat(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == paths.database_for("fixture-snapshot"):
            raise error
        return original_stat(path, *args, **kwargs)

    def open_pointer(path, *args: Any, **kwargs: Any) -> int:
        if path == target:
            raise error
        return original_open(path, *args, **kwargs)

    if where == "pointer":
        monkeypatch.setattr(database.os, "open", open_pointer)
    elif where == "manifest":
        monkeypatch.setattr(Path, "read_text", read)
    elif where == "stat":
        monkeypatch.setattr(Path, "stat", stat)
    else:
        monkeypatch.setattr(database.sqlite3, "connect", Mock(side_effect=error))
    with pytest.raises(CatalogImportError) as raised:
        await module.refresh_catalog(catalog_config)
    assert raised.value.__cause__ is error
    assert paths.active_pointer.read_bytes() == original_pointer
    assert paths.database_for("fixture-snapshot").read_bytes() == original_database
    download.assert_not_called()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


def test_source_probe_does_not_discard_cache_on_resource_failure(
    catalog_config, monkeypatch
) -> None:
    previous = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    original = sqlite3.OperationalError("unable to open source")
    original.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    resource = OSError(errno.EMFILE, "too many open files")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = original
    monkeypatch.setattr(
        module,
        "_verify_database",
        Mock(
            return_value=database._DatabaseVerification(
                status=_VerificationStatus.OPERATIONAL_FAILURE, error=resource
            )
        ),
    )
    with pytest.raises(OSError) as raised:
        module._copy_crosswalk(connection, previous)
    assert raised.value is resource
    assert raised.value.__cause__ is original


@pytest.mark.parametrize("directory", [False, True])
def test_verification_identifies_absent_or_nonfile_sources(tmp_path, directory) -> None:
    path = tmp_path / "missing.sqlite"
    if directory:
        path.mkdir()
    assert _verify_database(path).status is _VerificationStatus.MISSING


def test_verification_keeps_query_failure_when_close_also_fails(
    catalog_config, monkeypatch
) -> None:
    error = sqlite3.DatabaseError("original corruption")
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT_INDEX
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = error
    connection.close.side_effect = sqlite3.OperationalError("cleanup failure")
    monkeypatch.setattr(database.sqlite3, "connect", Mock(return_value=connection))
    result = _verify_database(
        CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    )
    assert result.status is _VerificationStatus.CORRUPT
    assert result.error is error
    connection.close.assert_called_once()


def test_journal_recovery_is_bounded_to_one_attempt(
    catalog_config, monkeypatch
) -> None:
    previous = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    error = sqlite3.OperationalError("journal still requires rollback")
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = error
    verify = Mock(
        return_value=database._DatabaseVerification(
            status=_VerificationStatus.RECOVERY_REQUIRED, error=error
        )
    )
    monkeypatch.setattr(module, "_verify_database", verify)
    with pytest.raises(sqlite3.OperationalError) as raised:
        module._copy_crosswalk(connection, previous)
    assert raised.value is error
    assert verify.call_args_list == [
        ((previous,),),
        ((previous,), {"recover_hot_journal": True}),
    ]
    connection.execute.assert_called_once()
