from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import subprocess
import sys
import traceback
import tracemalloc
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from rebrickable import CatalogStatus
from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.config import Config
from rebrickable.errors import CatalogImportError, CatalogSchemaError
from tests.test_hot_journal_recovery import seed_mapping
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


@pytest.mark.parametrize("when", ["initial", "locked"])
async def test_oversized_pointer_preserves_cache_and_activation_history(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    seed_mapping(catalog_config)
    sources = stable_sources(catalog_config)
    historical = paths.snapshots_dir / "historical-snapshot"
    historical.mkdir()
    payload = {
        "snapshot_id": "fixture-snapshot",
        "_activation_generation": "a" * 32,
        "_activation_history": ["fixture-snapshot", historical.name],
    }
    module._atomic_json(paths.active_pointer, payload)
    oversized = module._encode_json({**payload, "padding": "x" * 1024 * 1024})
    original_acquire = module.acquire_lock

    async def acquire(lock: Any) -> None:
        await original_acquire(lock)
        paths.active_pointer.write_bytes(oversized)

    fetch = Mock(side_effect=downloader(sources))
    carry = Mock(side_effect=AssertionError("oversized pointers must stop carryover"))
    monkeypatch.setattr(module, "_download_one", fetch)
    monkeypatch.setattr(module, "_carry_crosswalk", carry)
    if when == "initial":
        paths.active_pointer.write_bytes(oversized)
    else:
        monkeypatch.setattr(module, "acquire_lock", acquire)
    with pytest.raises(CatalogImportError, match="1 MiB limit") as raised:
        await module.refresh_catalog(catalog_config)
    assert str(paths.active_pointer) in str(raised.value)
    assert str(len(oversized)) in str(raised.value)
    assert paths.active_pointer.read_bytes() == oversized
    assert {child.name for child in paths.snapshots_dir.iterdir()} == {
        "fixture-snapshot",
        historical.name,
    }
    with closing(sqlite3.connect(paths.database_for("fixture-snapshot"))) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM api_crosswalk_cache"
        ).fetchone() == (1,)
    carry.assert_not_called()
    assert fetch.call_count == (0 if when == "initial" else 12)
    state = await database.catalog_state(catalog_config)
    assert state.status is CatalogStatus.UNREADABLE
    assert str(paths.active_pointer) in state.diagnostics[0]
    assert str(len(oversized)) in state.diagnostics[0]
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("force", [False, True])
async def test_pointer_capacity_fails_before_even_unchanged_downloads(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, *, force: bool
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    historical = paths.snapshots_dir / "historical-snapshot"
    historical.mkdir()
    module._atomic_json(
        paths.active_pointer,
        {
            "snapshot_id": "fixture-snapshot",
            "_activation_generation": "a" * 32,
            "_activation_history": ["fixture-snapshot", historical.name],
        },
    )
    before = paths.active_pointer.read_bytes()
    monkeypatch.setattr(module, "_MAX_POINTER_BYTES", len(before))
    fetch = Mock(side_effect=downloader(stable_sources(catalog_config), unchanged=True))
    monkeypatch.setattr(module, "_download_one", fetch)
    with pytest.raises(
        CatalogImportError, match="3 activation history entries"
    ) as raised:
        await module.refresh_catalog(catalog_config, force=force)
    assert str(paths.active_pointer) in str(raised.value)
    assert f"> {len(before)} bytes" in str(raised.value)
    assert paths.active_pointer.read_bytes() == before
    fetch.assert_not_called()
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


async def test_pointer_capacity_is_rebuilt_under_promotion_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    payload = {
        "snapshot_id": "fixture-snapshot",
        "_activation_generation": "a" * 32,
        "_activation_history": ["fixture-snapshot"],
    }
    module._atomic_json(paths.active_pointer, payload)
    historical = paths.snapshots_dir / f"history-{'x' * 85}"
    historical.mkdir()
    payload["_activation_history"].append(historical.name)
    expanded = module._encode_json(payload)
    assert len(expanded) <= 256
    monkeypatch.setattr(module, "_MAX_POINTER_BYTES", 256)
    original_acquire = module.acquire_lock

    async def acquire(lock: Any) -> None:
        await original_acquire(lock)
        paths.active_pointer.write_bytes(expanded)

    monkeypatch.setattr(module, "acquire_lock", acquire)
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )
    carry = Mock(
        side_effect=AssertionError("capacity must be checked before carryover")
    )
    monkeypatch.setattr(module, "_carry_crosswalk", carry)
    with pytest.raises(CatalogImportError, match="3 activation history entries"):
        await module.refresh_catalog(catalog_config)
    assert paths.active_pointer.read_bytes() == expanded
    carry.assert_not_called()
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


def test_small_pointer_peak_allocation_is_below_128_kib(catalog_config: Config) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    # Warm coverage instrumentation before measuring the reader's allocations.
    assert database._read_active_pointer(paths).snapshot_id == "fixture-snapshot"
    tracemalloc.start()
    try:
        pointer = database._read_active_pointer(paths)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert pointer.snapshot_id == "fixture-snapshot"
    assert peak < 128 * 1024


@pytest.mark.parametrize("growth", [1, 1000])
def test_pointer_growth_within_limit_is_read_through_eof(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, growth: int
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    original_fstat = os.fstat

    def grow(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        with paths.active_pointer.open("ab") as handle:
            handle.write(b" " * growth)
        return metadata

    monkeypatch.setattr(database.os, "fstat", grow)
    assert database._read_active_pointer(paths).snapshot_id == "fixture-snapshot"


@pytest.mark.parametrize("setting", [0, 640, 4300])
@pytest.mark.parametrize("digits", [4300, 4301])
@pytest.mark.parametrize("negative", [False, True])
def test_pointer_integer_boundary_ignores_global_limit(
    catalog_config: Config, setting: int, digits: int, *, negative: bool
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    integer = f"{'-' if negative else ''}{'9' * digits}"
    paths.active_pointer.write_text(
        f'{{"snapshot_id":"fixture-snapshot","unused":{integer}}}'
    )
    original_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(setting)
        pointer = database._read_active_pointer(paths)
    finally:
        sys.set_int_max_str_digits(original_limit)
    assert pointer.snapshot_id == ("fixture-snapshot" if digits <= 4300 else "")
    if digits <= 4300:
        assert pointer.data["unused"].bit_length() > 10_000
        assert (pointer.data["unused"] < 0) is negative


@pytest.mark.parametrize(
    "damage", ["device-link", "oversized", "malformed", "deep", "denied"]
)
async def test_uncertain_activation_markers_finish_refresh_and_release_lock(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    orphan = paths.snapshots_dir / "uncertain-orphan"
    orphan.mkdir()
    marker = orphan / module._ACTIVATION_MARKER
    original_read_text = Path.read_text

    def guarded_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == marker:
            raise AssertionError("activation marker must not use unbounded read_text")
        return original_read_text(path, *args, **kwargs)

    if damage == "device-link":
        if not await asyncio.to_thread(Path("/dev/zero").exists):
            pytest.skip("requires /dev/zero")
        try:
            marker.symlink_to("/dev/zero")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    elif damage == "oversized":
        marker.write_bytes(b" " * (1024 * 1024 + 1))
    elif damage == "deep":
        marker.write_text(f"{'[' * 10_000}0{']' * 10_000}")
    else:
        marker.write_text("malformed")
    if damage == "denied":
        original_open = os.open

        def denied(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if path == marker:
                raise PermissionError(errno.EACCES, "marker denied")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(database.os, "open", denied)
    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert not module._managed_snapshot(orphan, "generation")
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )
    await module.refresh_catalog(catalog_config)
    connection, state = await database.open_catalog(catalog_config)
    await connection.close()
    assert state.status is CatalogStatus.READY
    assert orphan.is_dir()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_fifo_activation_marker_cannot_block_promotion(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, os, sys
from pathlib import Path
from unittest.mock import patch
from rebrickable import refresh as module
from rebrickable.catalog.database import CatalogPaths, open_catalog
from tests.catalog_fixture import build_catalog_config
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader
async def main():
    config = build_catalog_config(Path(sys.argv[1]))
    paths = CatalogPaths.from_config(config)
    orphan = paths.snapshots_dir / 'fifo-orphan'
    orphan.mkdir()
    os.mkfifo(orphan / module._ACTIVATION_MARKER)
    with patch.object(module, '_download_one', downloader(stable_sources(config))):
        await module.refresh_catalog(config)
    connection, state = await open_catalog(config)
    await connection.close()
    assert orphan.is_dir()
asyncio.run(main())
""",
            str(tmp_path),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_activation_marker_exact_size_boundary(tmp_path: Path, extra: int) -> None:
    marker = tmp_path / module._ACTIVATION_MARKER
    payload = b'{"generation":"generation"}'
    length = database._MAX_POINTER_BYTES + extra
    marker.write_bytes(payload + b" " * (length - len(payload)))
    assert module._managed_snapshot(tmp_path, "generation") is (extra <= 0)


@pytest.mark.parametrize("stage", ["rollback", "detach"])
@pytest.mark.parametrize(
    "cleanup_type",
    [RuntimeError, MemoryError, KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_cache_carryover_cleanup_preserves_precedence_and_existing_chain(
    catalog_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    cleanup_type: type[BaseException],
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    original = sqlite3.OperationalError("original SQLite failure")
    prior_cause = RuntimeError("earlier source failure")
    original.__cause__ = prior_cause
    cleanup = cleanup_type("cleanup failure")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [None, original, cleanup]
    if stage == "rollback":
        connection.rollback.side_effect = cleanup
    monkeypatch.setattr(module.sqlite3, "connect", Mock(return_value=connection))
    verifier = Mock(side_effect=AssertionError("cleanup must abort before recovery"))
    monkeypatch.setattr(module, "_verify_database", verifier)
    ordinary = issubclass(cleanup_type, Exception)
    with pytest.raises(CatalogImportError if ordinary else cleanup_type) as raised:
        module._carry_crosswalk(source, source)
    if ordinary:
        assert raised.value.__cause__ is original
    else:
        assert raised.value is cleanup
        assert raised.value.__context__ is original
    assert original.__cause__ is prior_cause
    rendered = "".join(traceback.format_exception(raised.value))
    assert "original SQLite failure" in rendered
    assert "cleanup failure" in rendered
    assert "earlier source failure" in rendered
    verifier.assert_not_called()
    connection.close.assert_called_once()


def test_recovered_identity_mismatch_skips_second_attach(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = database.CatalogPaths.from_config(catalog_config).database_for(
        "fixture-snapshot"
    )
    hot_journal = sqlite3.OperationalError("hot journal")
    hot_journal.sqlite_errorcode = sqlite3.SQLITE_READONLY_ROLLBACK
    second_attach = sqlite3.OperationalError("second ATTACH must not happen")
    connection = Mock(spec=sqlite3.Connection)
    connection.execute.side_effect = [hot_journal, second_attach]
    verifier = Mock(
        side_effect=[
            database._DatabaseVerification(
                status=database._VerificationStatus.RECOVERY_REQUIRED
            ),
            database._DatabaseVerification(
                status=database._VerificationStatus.HEALTHY,
                snapshot_id="other",
                schema_version=1,
            ),
        ]
    )
    monkeypatch.setattr(module, "_verify_database", verifier)
    result = module._copy_crosswalk(
        connection,
        source,
        eligibility=database._CacheEligibility(
            identity_present=True, manifest_identity="expected"
        ),
    )
    assert result == "previous catalog identity disagrees with its manifest"
    connection.execute.assert_called_once()
    assert verifier.call_count == 2


@pytest.mark.parametrize("available", [False, True])
def test_archive_denied_contained_link_tracks_effective_nofollow(
    catalog_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    available: bool,
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    fingerprint = next(
        item
        for item in asyncio.run(database.catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    target = source.with_name("contained-target.gz")
    target.write_bytes(source.read_bytes())
    link = tmp_path / "symlink-probe"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    denied = PermissionError(errno.EACCES, "target denied")

    def deny(*_args: Any) -> int:
        source.unlink()
        source.symlink_to(target)
        raise denied

    monkeypatch.setattr(database.os, "O_NOFOLLOW", 1 if available else 0, raising=False)
    monkeypatch.setattr(database.os, "open", deny)
    if available:
        assert not module._reuse_archive(
            paths, "fixture-snapshot", source.name, tmp_path / "copy.gz", fingerprint
        )
    else:
        with pytest.raises(PermissionError) as raised:
            module._reuse_archive(
                paths,
                "fixture-snapshot",
                source.name,
                tmp_path / "copy.gz",
                fingerprint,
            )
        assert raised.value is denied


@pytest.mark.parametrize("schema_version", [None, 2])
async def test_schema_diagnostic_fallback_without_provenance(
    catalog_config: Config, monkeypatch: pytest.MonkeyPatch, schema_version: int | None
) -> None:
    from unittest.mock import AsyncMock  # noqa: PLC0415 - isolated state fallback

    from rebrickable import CatalogState  # noqa: PLC0415 - isolated state fallback

    state = CatalogState(
        status=CatalogStatus.SCHEMA_MISMATCH,
        database_path=catalog_config.database_path,
        manifest_path=database.CatalogPaths.from_config(catalog_config).active_pointer,
        schema_version=schema_version,
    )
    monkeypatch.setattr(database, "catalog_state", AsyncMock(return_value=state))
    with pytest.raises(CatalogSchemaError) as raised:
        await database.open_catalog(catalog_config)
    assert str(raised.value) == (
        "catalog schema does not match this release"
        if schema_version is None
        else "reported catalog schema version 2 does not match supported version 1"
    )
