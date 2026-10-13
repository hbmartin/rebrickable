from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import sys
import traceback
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from rebrickable import CatalogState, CatalogStatus, RebrickableSession
from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.config import Config
from rebrickable.errors import CatalogImportError, CatalogSchemaError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


def test_pointer_oversized_integer_is_invalid_and_releases_descriptor(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    original_limit = sys.get_int_max_str_digits()
    original_open = os.open
    descriptors = []

    def record(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    paths.active_pointer.write_text(
        '{"snapshot_id":"fixture-snapshot","unused":' + "1" * 4301 + "}"
    )
    monkeypatch.setattr(database.os, "open", record)
    try:
        sys.set_int_max_str_digits(4300)
        pointer = database._read_active_pointer(paths)
    finally:
        sys.set_int_max_str_digits(original_limit)
    assert pointer.snapshot_id == ""
    assert pointer.error is None
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_pointer_size_boundary(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    prefix = b'{"snapshot_id":"fixture-snapshot","padding":"'
    suffix = b'"}'
    length = database._MAX_POINTER_BYTES + extra
    paths.active_pointer.write_bytes(
        prefix + b"x" * (length - len(prefix) - len(suffix)) + suffix
    )
    original_fdopen = os.fdopen
    handles = []

    def fdopen(*args: Any, **kwargs: Any) -> Mock:
        handle = Mock(wraps=original_fdopen(*args, **kwargs))
        handles.append(handle)
        return handle

    monkeypatch.setattr(database.os, "fdopen", fdopen)
    pointer = database._read_active_pointer(paths)
    assert pointer.snapshot_id == ("fixture-snapshot" if extra <= 0 else "")
    if extra > 0:
        handles[0].read.assert_not_called()
    else:
        handles[0].read.assert_called_once_with(length + 1)
    handles[0].close.assert_called_once()


def test_pointer_growth_after_descriptor_inspection_is_bounded(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    original_fstat = os.fstat
    original_fdopen = os.fdopen
    reads = []

    def fstat(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        with paths.active_pointer.open("ab") as handle:
            handle.write(b" " * (database._MAX_POINTER_BYTES + 1 - metadata.st_size))
        return metadata

    def fdopen(*args: Any, **kwargs: Any) -> Mock:
        handle = Mock(wraps=original_fdopen(*args, **kwargs))
        reads.append(handle.read)
        return handle

    monkeypatch.setattr(database.os, "fstat", fstat)
    monkeypatch.setattr(database.os, "fdopen", fdopen)
    assert database._read_active_pointer(paths).snapshot_id == ""
    assert sum(call.args[0] for call in reads[0].call_args_list) == (
        database._MAX_POINTER_BYTES + 1
    )
    assert all(0 < call.args[0] <= 65_536 for call in reads[0].call_args_list)


async def test_refresh_preserves_orphan_with_deep_activation_marker(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    orphan = paths.snapshots_dir / "unproven-orphan"
    orphan.mkdir()
    (orphan / module._ACTIVATION_MARKER).write_text("[" * 10_000 + "0" + "]" * 10_000)
    assert not module._managed_snapshot(orphan, "generation")
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )
    report = await module.refresh_catalog(catalog_config)
    state = await database.catalog_state(catalog_config, verify=True)
    assert state.status is CatalogStatus.READY
    assert state.snapshot_id == report.snapshot_id
    assert orphan.is_dir()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("extra", [-1, 0])
async def test_promotion_enforces_exact_encoded_pointer_limit(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    generation = "a" * 32
    legacy = "uncertain-é"
    (paths.snapshots_dir / legacy).mkdir()
    module._atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": generation,
            "_activation_history": ["fixture-snapshot", legacy],
        },
    )
    before = paths.active_pointer.read_bytes()
    original_promote = module._promote_snapshot
    carry = Mock(wraps=module._carry_crosswalk)
    move = Mock(wraps=module.shutil.move)
    expected = []

    def promote(paths, **kwargs: Any) -> str | None:
        payload = {
            "snapshot_id": kwargs["destination"].name,
            "_activation_generation": generation,
            "_activation_history": [
                kwargs["destination"].name,
                "fixture-snapshot",
                legacy,
            ],
        }
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        expected.append(encoded)
        monkeypatch.setattr(module, "_MAX_POINTER_BYTES", len(encoded) + extra)
        return original_promote(paths, **kwargs)

    monkeypatch.setattr(module, "_promote_snapshot", promote)
    monkeypatch.setattr(module, "_carry_crosswalk", carry)
    monkeypatch.setattr(module.shutil, "move", move)
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )
    if extra < 0:
        with pytest.raises(CatalogImportError, match="exceeds the 1 MiB limit"):
            await module.refresh_catalog(catalog_config)
        assert paths.active_pointer.read_bytes() == before
        assert paths.database_for("fixture-snapshot").is_file()
        assert (paths.snapshots_dir / legacy).is_dir()
        carry.assert_not_called()
        move.assert_not_called()
    else:
        await module.refresh_catalog(catalog_config)
        assert paths.active_pointer.read_bytes() == expected[0]
        carry.assert_called_once()
        move.assert_called_once()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))


@pytest.mark.parametrize("stage", ["rollback", "detach"])
@pytest.mark.parametrize(
    "cleanup_type",
    [RuntimeError, KeyboardInterrupt, SystemExit, MemoryError, asyncio.CancelledError],
)
def test_copy_cleanup_exposes_both_failures_and_aborts_recovery(
    catalog_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    cleanup_type: type[BaseException],
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    original = sqlite3.OperationalError("original source failure")
    original.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
    cleanup = cleanup_type("secondary cleanup failure")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [None, original, cleanup]
    if stage == "rollback":
        connection.rollback.side_effect = cleanup
    verifier = Mock(side_effect=AssertionError("failed cleanup must abort recovery"))
    monkeypatch.setattr(module, "_verify_database", verifier)
    ordinary = issubclass(cleanup_type, Exception)
    with pytest.raises(
        sqlite3.OperationalError if ordinary else cleanup_type
    ) as raised:
        module._copy_crosswalk(connection, source)
    assert raised.value is (original if ordinary else cleanup)
    if ordinary:
        assert raised.value.__cause__ is None
        assert any(stage.lower() in note.lower() for note in raised.value.__notes__)
    else:
        assert raised.value.__context__ is original
    rendered = "".join(traceback.format_exception(raised.value))
    assert "original source failure" in rendered
    assert "secondary cleanup failure" in rendered
    verifier.assert_not_called()


@pytest.mark.parametrize("eager", [False, True])
@pytest.mark.parametrize("source", ["database", "manifest"])
async def test_schema_errors_report_found_and_supported_versions(
    catalog_config: Config, source: str, *, eager: bool
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    if source == "database":
        with closing(
            sqlite3.connect(paths.database_for("fixture-snapshot"))
        ) as connection:
            connection.execute(
                "UPDATE snapshot_meta SET value='2' WHERE key='schema_version'"
            )
            connection.commit()
    else:
        manifest = paths.manifest_for("fixture-snapshot")
        payload = json.loads(manifest.read_text())
        payload["schema_version"] = 2
        manifest.write_text(json.dumps(payload))
    opener = RebrickableSession.connect if eager else database.open_catalog
    with pytest.raises(CatalogSchemaError) as raised:
        await opener(catalog_config)
    assert str(raised.value) == (
        "catalog database schema version 2 does not match supported version 1"
        if source == "database"
        else "catalog manifest declares schema version 2; supported version is 1"
    )


async def test_schema_error_keeps_generic_fallback_when_version_unknown(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = CatalogState(
        status=CatalogStatus.SCHEMA_MISMATCH,
        database_path=catalog_config.database_path,
        manifest_path=database.CatalogPaths.from_config(catalog_config).active_pointer,
    )
    monkeypatch.setattr(database, "catalog_state", AsyncMock(return_value=state))
    with pytest.raises(
        CatalogSchemaError, match=r"^catalog schema does not match this release$"
    ):
        await database.open_catalog(catalog_config)


def test_archive_open_denial_rechecks_changed_snapshot_containment(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    previous = next(
        item
        for item in asyncio.run(database.catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    snapshot = paths.snapshots_dir / "fixture-snapshot"
    source = snapshot / "parts.csv.gz"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / source.name).write_bytes(source.read_bytes())
    # Probe symlink support before altering the snapshot.
    link = tmp_path / "test-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    link.unlink()
    original_stat = Path.stat
    opened = False

    def denied(*_args: Any) -> int:
        nonlocal opened
        opened = True
        snapshot.rename(snapshot.with_name("old-snapshot"))
        snapshot.symlink_to(outside, target_is_directory=True)
        raise PermissionError(errno.EACCES, "opening denied after snapshot moved")

    def stat(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if opened and path == source:
            raise PermissionError(errno.EACCES, "archive probe denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(database.os, "open", denied)
    monkeypatch.setattr(Path, "stat", stat)
    assert not module._reuse_archive(
        paths, "fixture-snapshot", source.name, tmp_path / "copy.gz", previous
    )
    assert not (tmp_path / "copy.gz").exists()
