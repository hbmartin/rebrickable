from __future__ import annotations

import asyncio
import errno
import json
import os
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

from rebrickable import CatalogStatus, RebrickableSession
from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.catalog.database import CatalogPaths, catalog_state, open_catalog
from rebrickable.catalog.schema import _REQUIRED_COLUMNS, SCHEMA_SQL
from rebrickable.errors import CatalogImportError, CatalogUnreadableError
from tests.test_hot_journal_recovery import crash_writer, seed_mapping
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


async def test_broken_required_view_is_structural_and_replaceable(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    previous = paths.database_for("fixture-snapshot")
    with closing(sqlite3.connect(previous)) as connection:
        connection.executescript(
            "DROP VIEW latest_inventories; CREATE VIEW latest_inventories AS "
            "SELECT id, version, owner_num FROM missing_table;"
        )
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    result = database._verify_database(previous)
    assert result.status is database._VerificationStatus.CORRUPT
    assert result.error.sqlite_errorcode == sqlite3.SQLITE_ERROR
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    await module.refresh_catalog(catalog_config)
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY


@pytest.mark.parametrize("copy", [False, True])
@pytest.mark.parametrize("manifest_identity", [False, True])
async def test_restored_directory_preserves_logical_identity(
    catalog_config, copy, manifest_identity
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original = paths.snapshots_dir / "fixture-snapshot"
    restored = original.with_name("restored")
    if copy:
        shutil.copytree(original, restored)
    else:
        original.rename(restored)
    manifest = restored / "manifest.json"
    if not manifest_identity:
        data = json.loads(manifest.read_text())
        del data["snapshot_id"]
        manifest.write_text(json.dumps(data))
    paths.active_pointer.write_text(json.dumps({"snapshot_id": restored.name}))
    state = await catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.READY
    assert state.snapshot_id == "restored"
    async with await RebrickableSession.connect(catalog_config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"


@pytest.mark.parametrize("damage", ["missing", "invalid", "directory", "loop"])
async def test_manifest_damage_preserves_verified_mapping_cache(
    catalog_config, monkeypatch, damage
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    seed_mapping(catalog_config)
    manifest = paths.manifest_for("fixture-snapshot")
    manifest.unlink()
    if damage == "invalid":
        manifest.write_text("invalid JSON")
    elif damage == "directory":
        manifest.mkdir()
    elif damage == "loop":
        manifest.symlink_to(manifest.name)
    events = []
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    report = await module.refresh_catalog(catalog_config, callback=events.append)
    with closing(sqlite3.connect(paths.database_for(report.snapshot_id))) as connection:
        assert connection.execute(
            "SELECT external_id FROM api_crosswalk_cache"
        ).fetchall() == [("journal-mapping",)]
        assert connection.execute(
            "SELECT source_id FROM user_mapping_overrides WHERE entity_kind='part'"
        ).fetchone() == ("3001",)
    assert not any("skipped cached" in event.message for event in events)
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY


@pytest.mark.parametrize("retain", [1, 2, 3])
@pytest.mark.parametrize("target", ["manifest", "database", "snapshot"])
async def test_active_loop_keeps_generation_and_activation_history(
    catalog_config, monkeypatch, retain, target
) -> None:
    config = replace(catalog_config, snapshot_retention=retain)
    paths = CatalogPaths.from_config(config)
    sources = stable_sources(config)
    generation = uuid.uuid4().hex
    module._atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": generation,
            "_activation_history": ["fixture-snapshot"],
        },
    )
    path = {
        "manifest": paths.manifest_for("fixture-snapshot"),
        "database": paths.database_for("fixture-snapshot"),
        "snapshot": paths.snapshots_dir / "fixture-snapshot",
    }[target]
    if target == "snapshot":
        shutil.rmtree(path)
    else:
        path.unlink()
    path.symlink_to(path.name)
    pointer = database._read_active_pointer(paths)
    assert pointer.activation_id == "fixture-snapshot"
    assert pointer.data["_activation_generation"] == generation
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    for _ in range(2):
        await module.refresh_catalog(config)
        payload = json.loads(paths.active_pointer.read_text())
        assert payload["_activation_generation"] == generation
        assert "fixture-snapshot" in payload["_activation_history"]
        assert path.is_symlink()


@pytest.mark.parametrize("damage", ["directory", "parent-file", "parent-link"])
async def test_pointer_obstruction_fails_before_download(
    catalog_config, tmp_path, monkeypatch, damage
) -> None:
    config = catalog_config
    if damage == "directory":
        pointer = CatalogPaths.from_config(config).active_pointer
        pointer.unlink()
        pointer.mkdir()
    else:
        parent = tmp_path / "obstructed"
        if damage == "parent-file":
            parent.write_bytes(b"not a directory")
        else:
            parent.symlink_to(tmp_path / "missing", target_is_directory=True)
        config = replace(config, database_path=parent / "catalog.sqlite")
    download = Mock()
    monkeypatch.setattr(module, "_download_one", download)
    with pytest.raises(CatalogImportError, match="unable to activate"):
        await module.refresh_catalog(config)
    download.assert_not_called()
    assert not tuple(config.cache_path.glob("refresh-*"))


async def test_pointer_destination_is_rechecked_before_install(
    catalog_config, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )

    def obstruct(event) -> None:
        if event.message == "promoting snapshot":
            paths.active_pointer.unlink()
            paths.active_pointer.mkdir()

    with pytest.raises(CatalogImportError, match="unable to activate"):
        await module.refresh_catalog(catalog_config, callback=obstruct)
    assert {path.name for path in paths.snapshots_dir.iterdir()} == {"fixture-snapshot"}
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_pointer_fifo_during_promotion_cannot_block_cleanup(catalog_config) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, os, sys
from pathlib import Path
import pytest_socket
import rebrickable.refresh as module
from rebrickable.config import Config
from rebrickable.catalog.database import CatalogPaths
from rebrickable.errors import CatalogImportError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader
pytest_socket.disable_socket(allow_unix_socket=True)
config = Config(database_path=Path(sys.argv[1]), cache_path=Path(sys.argv[2]))
paths = CatalogPaths.from_config(config)
module._download_one = downloader(stable_sources(config))
def obstruct(event):
    if event.message == 'promoting snapshot':
        paths.active_pointer.unlink()
        os.mkfifo(paths.active_pointer)
async def refresh():
    try:
        await module.refresh_catalog(config, callback=obstruct)
    except CatalogImportError:
        pass
    else:
        raise AssertionError('pointer obstruction was ignored')
asyncio.run(refresh())
assert {child.name for child in paths.snapshots_dir.iterdir()} == {'fixture-snapshot'}
assert not tuple(config.cache_path.glob('refresh-*'))
""",
            str(catalog_config.database_path),
            str(catalog_config.cache_path),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""


async def test_open_catalog_explains_hot_journal_recovery(catalog_config) -> None:
    path = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    crash_writer(path)
    with pytest.raises(CatalogUnreadableError, match="rebrickable refresh"):
        await open_catalog(catalog_config)
    assert await asyncio.to_thread(Path(f"{path}-journal").exists)


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR_READ, sqlite3.SQLITE_ERROR])
def test_verified_recovery_requirement_permits_one_repair(
    catalog_config, monkeypatch, code
) -> None:
    error = sqlite3.OperationalError("source copy failed")
    error.sqlite_errorcode = code
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [error, None, None, None, None]
    verify = Mock(
        side_effect=[
            database._DatabaseVerification(
                status=database._VerificationStatus.RECOVERY_REQUIRED
            ),
            database._DatabaseVerification(status=database._VerificationStatus.HEALTHY),
        ]
    )
    monkeypatch.setattr(module, "_verify_database", verify)
    monkeypatch.setattr(
        module,
        "_catalog_metadata",
        Mock(
            return_value=database._DatabaseVerification(
                status=database._VerificationStatus.HEALTHY
            )
        ),
    )
    source = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    assert not module._copy_crosswalk(connection, source)
    assert verify.call_args_list[1].kwargs == {"recover_hot_journal": True}
    connection.rollback.assert_called_once()


@pytest.mark.parametrize(
    "error", [RuntimeError("original failure"), asyncio.CancelledError()]
)
@pytest.mark.parametrize(
    "cleanup_error",
    [
        sqlite3.OperationalError("secondary failure"),
        RuntimeError("secondary failure"),
        asyncio.CancelledError(),
    ],
)
def test_carryover_cleanup_preserves_any_propagating_exception(
    monkeypatch, error, cleanup_error
) -> None:
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = error
    connection.close.side_effect = cleanup_error
    monkeypatch.setattr(module.sqlite3, "connect", Mock(return_value=connection))
    with pytest.raises(type(error)) as raised:
        module._carry_crosswalk(None, Path("unused.sqlite"))
    assert raised.value is error
    connection.close.assert_called_once()


def test_unexpected_close_failure_after_success_is_structured(monkeypatch) -> None:
    connection = Mock(spec=sqlite3.Connection)
    error = RuntimeError("close failed")
    connection.close.side_effect = error
    monkeypatch.setattr(module.sqlite3, "connect", Mock(return_value=connection))
    with pytest.raises(
        CatalogImportError, match="unable to close mapping cache"
    ) as raised:
        module._carry_crosswalk(None, Path("unused.sqlite"))
    assert raised.value.__cause__ is error


def test_required_schema_contract_matches_schema_sql() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(SCHEMA_SQL)
        objects = dict(
            connection.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        )
        assert set(_REQUIRED_COLUMNS) == {
            name for name in objects if not name.startswith("search_fts_")
        }
        for name, (kind, columns) in _REQUIRED_COLUMNS.items():
            assert objects[name] == kind
            assert columns == {
                row[1] for row in connection.execute(f"PRAGMA table_info('{name}')")
            }


@pytest.mark.parametrize("missing", [False, True])
def test_database_requires_nonempty_logical_identity(catalog_config, missing) -> None:
    path = CatalogPaths.from_config(catalog_config).database_for("fixture-snapshot")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "DELETE FROM snapshot_meta WHERE key='snapshot_id'"
            if missing
            else "UPDATE snapshot_meta SET value='' WHERE key='snapshot_id'"
        )
        connection.commit()
    assert (
        database._verify_database(path).status is database._VerificationStatus.CORRUPT
    )


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "denied"),
        OSError(errno.EMFILE, "descriptors exhausted"),
    ],
)
async def test_manifest_damage_does_not_hide_operational_database_failure(
    catalog_config, monkeypatch, error
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    previous = paths.database_for("fixture-snapshot").read_bytes()
    paths.manifest_for("fixture-snapshot").write_text("invalid JSON")
    monkeypatch.setattr(module, "_download_one", downloader(sources))
    monkeypatch.setattr(
        module,
        "_verify_database",
        Mock(
            return_value=database._DatabaseVerification(
                status=database._VerificationStatus.OPERATIONAL_FAILURE, error=error
            )
        ),
    )
    monkeypatch.setattr(database, "_verify_database", module._verify_database)
    with pytest.raises(
        CatalogImportError, match="unable to verify previous catalog"
    ) as raised:
        await module.refresh_catalog(catalog_config)
    assert raised.value.__cause__ is error
    assert paths.active_pointer.read_bytes() == pointer
    assert paths.database_for("fixture-snapshot").read_bytes() == previous
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
