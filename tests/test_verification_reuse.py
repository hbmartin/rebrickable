from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from rebrickable import CatalogStatus
from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.catalog.schema import SCHEMA_SQL
from rebrickable.errors import CatalogImportError, CatalogSchemaError
from tests.test_hot_journal_recovery import crash_writer, seed_mapping
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


@pytest.mark.parametrize("damage", ["schema", "files", "date", "file-fields"])
async def test_partial_manifest_identity_still_excludes_cache(
    catalog_config, monkeypatch, damage
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    manifest = paths.manifest_for("fixture-snapshot")
    payload = json.loads(manifest.read_text())
    payload["snapshot_id"] = "other-logical-snapshot"
    if damage == "schema":
        payload["schema_version"] = 2
    elif damage == "files":
        del payload["files"]
    elif damage == "date":
        payload["retrieved_at"] = "invalid-date"
    else:
        payload["files"] = [{}]
    manifest.write_text(json.dumps(payload))
    verify = Mock(wraps=database._verify_database)
    monkeypatch.setattr(database, "_verify_database", verify)
    monkeypatch.setattr(module, "_verify_database", verify)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    events = []
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    assert verify.call_count == 1
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM user_mapping_overrides"
        ).fetchone() == (2,)
    assert [event.message for event in events if "skipped cached" in event.message] == [
        "previous catalog identity disagrees with its manifest; skipped cached API mappings"
    ]


@pytest.mark.parametrize("when", ["initial", "during"])
async def test_recovered_identity_is_checked_before_copying(
    catalog_config, monkeypatch, when
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    source = paths.database_for("fixture-snapshot")
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)

    def damage() -> None:
        with closing(sqlite3.connect(source)) as connection:
            connection.execute(
                "UPDATE snapshot_meta SET value='other' WHERE key='snapshot_id'"
            )
            connection.commit()
        crash_writer(source)

    if when == "initial":
        damage()
    original = module._inspect_catalog

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> database._CatalogInspection:
        if when == "during" and not verify:
            damage()
        return await original(config, pointer, verify=verify, verification=verification)

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    events = []
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM user_mapping_overrides"
        ).fetchone() == (2,)
    assert any("identity disagrees" in event.message for event in events)
    assert not source.exists()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("change", ["replace", "modify"])
@pytest.mark.parametrize("manifest_damage", [False, True])
async def test_changed_database_invalidates_initial_verification(
    catalog_config, monkeypatch, change, manifest_damage
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    source = paths.database_for("fixture-snapshot")
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    original = module._inspect_catalog
    verify_calls = Mock(wraps=database._verify_database)
    monkeypatch.setattr(database, "_verify_database", verify_calls)
    monkeypatch.setattr(module, "_verify_database", verify_calls)

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> database._CatalogInspection:
        if not verify:
            replacement = (
                source.with_name("replacement.sqlite")
                if change == "replace"
                else source
            )
            if change == "replace":
                shutil.copy2(source, replacement)
            with closing(sqlite3.connect(replacement)) as connection:
                connection.execute(
                    "UPDATE snapshot_meta SET value='changed' WHERE key='snapshot_id'"
                )
                connection.commit()
            if change == "replace":
                os.replace(replacement, source)
            if manifest_damage:
                payload = json.loads(paths.manifest_for("fixture-snapshot").read_text())
                payload.pop("files")
                paths.manifest_for("fixture-snapshot").write_text(json.dumps(payload))
        return await original(config, pointer, verify=verify, verification=verification)

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    report = await module.refresh_catalog(catalog_config)
    assert verify_calls.call_count == 2
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)


@pytest.mark.parametrize("suffix", ["-journal", "-wal"])
def test_sidecar_changes_invalidate_reuse(catalog_config, suffix) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    verification = database._verify_database(source)
    assert database._verification_reusable(verification, source)
    sidecar = source.with_name(source.name + suffix)
    sidecar.write_bytes(b"changed")
    assert not database._verification_reusable(verification, source)
    sidecar.unlink()
    assert database._verification_reusable(verification, source)
    sidecar.mkdir()
    assert not database._verification_reusable(verification, source)


def test_change_during_verification_cannot_be_reused(
    catalog_config, monkeypatch
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    original = database._catalog_problem

    def problem(connection) -> database._DatabaseVerification:
        result = original(connection)
        metadata = source.stat()
        os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setattr(database, "_catalog_problem", problem)
    result = database._verify_database(source)
    assert result.status is database._VerificationStatus.HEALTHY
    assert result.signature is None
    assert not database._verification_reusable(result, source)


async def test_reverification_operational_failure_aborts_promotion(
    catalog_config, monkeypatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    source = paths.database_for("fixture-snapshot")
    before = paths.active_pointer.read_bytes()
    original = module._inspect_catalog
    sources = stable_sources(catalog_config)
    error = PermissionError(errno.EACCES, "source denied")

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> database._CatalogInspection:
        if not verify:
            source.touch()
            monkeypatch.setattr(
                database,
                "_verify_database",
                Mock(
                    return_value=database._DatabaseVerification(
                        status=database._VerificationStatus.OPERATIONAL_FAILURE,
                        error=error,
                    )
                ),
            )
        return await original(config, pointer, verify=verify, verification=verification)

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    with pytest.raises(CatalogImportError) as raised:
        await module.refresh_catalog(catalog_config)
    assert raised.value.__cause__ is error
    assert paths.active_pointer.read_bytes() == before
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))


@pytest.mark.parametrize("change", ["identity", "unsupported", "utf8"])
def test_attached_source_metadata_controls_copy(
    catalog_config, tmp_path, change
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    seed_mapping(catalog_config)
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(
            {
                "identity": "UPDATE snapshot_meta SET value='other' WHERE key='snapshot_id';",
                "unsupported": "UPDATE snapshot_meta SET value='2' WHERE key='schema_version'; DELETE FROM snapshot_meta WHERE key='snapshot_id';",
                "utf8": "UPDATE snapshot_meta SET value=CAST(x'ff' AS TEXT) WHERE key='snapshot_id';",
            }[change]
        )
    with closing(sqlite3.connect(tmp_path / "candidate.sqlite", uri=True)) as candidate:
        candidate.executescript(SCHEMA_SQL)
        reason = module._copy_crosswalk(
            candidate,
            source,
            eligibility=database._CacheEligibility(
                identity_present=True, manifest_identity="fixture-snapshot"
            ),
        )
        assert (
            reason
            == {
                "identity": "previous catalog identity disagrees with its manifest",
                "unsupported": "previous catalog has an unsupported database schema",
                "utf8": "previous catalog is unreadable",
            }[change]
        )
        assert not candidate.in_transaction
        assert len(candidate.execute("PRAGMA database_list").fetchall()) == 1
        assert candidate.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (0,)


@pytest.mark.parametrize("identity", [None, "", "other"])
async def test_unsupported_version_precedes_v1_identity_contract(
    catalog_config, identity
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "UPDATE snapshot_meta SET value='2' WHERE key='schema_version'"
        )
        connection.execute("DELETE FROM snapshot_meta WHERE key='snapshot_id'")
        if identity is not None:
            connection.execute(
                "INSERT INTO snapshot_meta VALUES ('snapshot_id', ?)", (identity,)
            )
        connection.commit()
    state = await database.catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.SCHEMA_MISMATCH
    assert state.schema_version == 2
    with pytest.raises(CatalogSchemaError):
        await database.open_catalog(catalog_config)


@pytest.mark.parametrize("field", ["schema_version", "snapshot_id"])
def test_invalid_metadata_utf8_is_structural(catalog_config, field) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "UPDATE snapshot_meta SET value=CAST(x'ff' AS TEXT) WHERE key=?", (field,)
        )
        connection.commit()
    result = database._verify_database(source)
    assert result.status is database._VerificationStatus.CORRUPT
    assert isinstance(result.error, UnicodeDecodeError)


@pytest.mark.parametrize(
    "definition",
    [
        "SELECT id, version, owner_num FROM inventories UNION ALL SELECT id, version FROM inventories",
        "SELECT id, version, owner_num FROM inventories a JOIN inventories b",
        "SELECT id, version FROM inventories",
    ],
)
def test_definite_view_damage_is_replaceable(catalog_config, definition) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(
            "DROP VIEW latest_inventories; CREATE VIEW latest_inventories(id, version, owner_num) AS "
            + definition
        )
    result = database._verify_database(source)
    assert result.status is database._VerificationStatus.CORRUPT
    assert isinstance(result.error, sqlite3.Error)


@pytest.mark.parametrize("problem", ["escape", "loop", "operational"])
async def test_verification_rechecks_containment_before_opening(
    catalog_config, monkeypatch, tmp_path, problem
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    pointer = database._read_active_pointer(paths)
    source = paths.database_for("fixture-snapshot")
    if problem == "escape":
        outside = tmp_path / "outside.sqlite"
        shutil.copy2(source, outside)
        source.unlink()
        source.symlink_to(outside)
    else:
        error = OSError(errno.ELOOP if problem == "loop" else errno.EMFILE, problem)
        monkeypatch.setattr(database, "_database_contained", Mock(side_effect=error))
    verifier = Mock(side_effect=AssertionError("unsafe database was opened"))
    monkeypatch.setattr(database, "_verify_database", verifier)
    inspection = await database._inspect_catalog(catalog_config, pointer, verify=True)
    assert inspection.state.status is CatalogStatus.UNREADABLE
    assert (inspection.error is not None) == (problem == "operational")
    verifier.assert_not_called()


async def test_repaired_manifest_reuses_unchanged_database(
    catalog_config, monkeypatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    manifest = paths.manifest_for("fixture-snapshot")
    payload = manifest.read_text()
    sources = stable_sources(catalog_config)
    manifest.write_text("invalid JSON")
    original = module._inspect_catalog
    verifier = Mock(wraps=database._verify_database)
    monkeypatch.setattr(database, "_verify_database", verifier)
    monkeypatch.setattr(module, "_verify_database", verifier)

    async def inspect(
        config, pointer, *, verify=False, verification=None
    ) -> database._CatalogInspection:
        if not verify:
            manifest.write_text(payload)
        return await original(config, pointer, verify=verify, verification=verification)

    monkeypatch.setattr(module, "_inspect_catalog", inspect)
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    await module.refresh_catalog(catalog_config)
    assert verifier.call_count == 1


def test_copy_cleanup_keeps_original_sqlite_failure(
    catalog_config, monkeypatch
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    original = sqlite3.OperationalError("source failure")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = original
    connection.rollback.side_effect = RuntimeError("unexpected cleanup failure")
    verifier = Mock(
        side_effect=AssertionError("failed cleanup must abort before probing")
    )
    monkeypatch.setattr(module, "_verify_database", verifier)
    with pytest.raises(sqlite3.OperationalError) as raised:
        module._copy_crosswalk(connection, source)
    assert raised.value is original
    verifier.assert_not_called()
