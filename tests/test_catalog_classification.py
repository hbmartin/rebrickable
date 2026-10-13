from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any
from unittest.mock import Mock

import pytest

from rebrickable import CatalogStatus
from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.errors import CatalogImportError
from tests.test_hot_journal_recovery import seed_mapping
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


@pytest.mark.parametrize(
    "damage", ["missing", "malformed", "schema", "identity", "directory", "loop"]
)
async def test_metadata_changes_preserve_raw_verification_and_cache_policy(
    catalog_config, monkeypatch, damage
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    original_inspect = module._inspect_catalog
    verify = Mock(wraps=database._verify_database)
    monkeypatch.setattr(database, "_verify_database", verify)
    monkeypatch.setattr(module, "_verify_database", verify)
    observed = []

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> database._CatalogInspection:
        if not verify:
            manifest = paths.manifest_for("fixture-snapshot")
            payload = json.loads(manifest.read_text())
            if damage in {"missing", "directory", "loop"}:
                manifest.unlink()
                if damage == "directory":
                    manifest.mkdir()
                elif damage == "loop":
                    manifest.symlink_to(manifest.name)
            elif damage == "malformed":
                manifest.write_text("invalid")
            else:
                payload["schema_version" if damage == "schema" else "snapshot_id"] = (
                    2 if damage == "schema" else "different-logical-identity"
                )
                manifest.write_text(json.dumps(payload))
            # Re-resolve metadata independently of access eligibility.
            pointer = database._read_active_pointer(paths)
        result = await original_inspect(
            config, pointer, verify=verify, verification=verification
        )
        observed.append(result)
        return result

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    events = []
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    assert verify.call_count == 1
    assert observed[-1].verification is observed[0].verification
    assert observed[-1].verification.status is database._VerificationStatus.HEALTHY
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0 if damage == "identity" else 1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM user_mapping_overrides"
        ).fetchone() == (2,)
    notices = [event.message for event in events if "skipped cached" in event.message]
    assert notices == (
        [
            "previous catalog identity disagrees with its manifest; skipped cached API mappings"
        ]
        if damage == "identity"
        else []
    )
    assert observed[-1].state.status is (
        CatalogStatus.SCHEMA_MISMATCH
        if damage == "schema"
        else (
            CatalogStatus.IMPORT_REQUIRED
            if damage == "missing"
            else CatalogStatus.UNREADABLE
        )
    )


@pytest.mark.parametrize("version", ["2", "0", "01", "bad", None])
async def test_unsupported_schema_has_distinct_copy_exclusion(
    catalog_config, monkeypatch, version
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    with closing(sqlite3.connect(paths.database_for("fixture-snapshot"))) as connection:
        connection.execute(
            "UPDATE snapshot_meta SET value=? WHERE key='schema_version'",
            (version or "",),
        )
        if version == "2":
            connection.execute("DROP VIEW latest_inventories")
        connection.commit()
    verification = database._verify_database(paths.database_for("fixture-snapshot"))
    assert verification.status is (
        database._VerificationStatus.INCOMPATIBLE
        if version == "2"
        else database._VerificationStatus.CORRUPT
    )
    assert (await database.catalog_state(catalog_config, verify=True)).status is (
        CatalogStatus.SCHEMA_MISMATCH if version == "2" else CatalogStatus.UNREADABLE
    )
    events = []
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)
    notice = next(
        event.message for event in events if "skipped cached" in event.message
    )
    assert ("unsupported database schema" in notice) == (version == "2")


async def test_missing_sqlite_feature_is_operational(
    catalog_config, monkeypatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    source = paths.database_for("fixture-snapshot")
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(
            "DROP VIEW latest_inventories; CREATE VIEW latest_inventories AS "
            "SELECT id, version, missing_function(owner_num) AS owner_num FROM inventories;"
        )
    result = database._verify_database(source)
    assert result.status is database._VerificationStatus.OPERATIONAL_FAILURE
    pointer = paths.active_pointer.read_bytes()
    download = Mock()
    monkeypatch.setattr(module, "_download_one", download)
    with pytest.raises(CatalogImportError, match="missing_function"):
        await module.refresh_catalog(catalog_config)
    download.assert_not_called()
    assert paths.active_pointer.read_bytes() == pointer


@pytest.mark.parametrize(
    "message",
    [
        "no such module: fts5",
        "no such collation sequence: missing",
        "unknown environment failure",
        "no such table: absent",
        "no such column: absent",
        "view latest_inventories is circularly defined",
    ],
)
def test_schema_error_classification_preserves_cause(
    catalog_config, monkeypatch, message
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    monkeypatch.setattr(database, "_catalog_problem", Mock(side_effect=error))
    result = database._verify_database(paths.database_for("fixture-snapshot"))
    assert result.error is error
    assert result.status is (
        database._VerificationStatus.CORRUPT
        if message.startswith(("no such table:", "no such column:", "view "))
        else database._VerificationStatus.OPERATIONAL_FAILURE
    )


def test_metadata_is_read_once(catalog_config, monkeypatch) -> None:
    statements = []
    connect = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", traced)
    result = database._verify_database(
        database.CatalogPaths.from_config(catalog_config).database_for(
            "fixture-snapshot"
        )
    )
    assert result.snapshot_id == "fixture-snapshot"
    assert sum("FROM snapshot_meta" in sql for sql in statements) == 1
