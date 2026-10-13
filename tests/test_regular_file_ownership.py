from __future__ import annotations

import asyncio
import errno
import io
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Never
from unittest.mock import Mock

import pytest

from rebrickable import refresh as module
from rebrickable.catalog import database


@pytest.mark.parametrize("regular", [False, True])
def test_owned_close_does_not_close_recycled_descriptor(
    catalog_config, monkeypatch, tmp_path, regular
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    if regular:
        read_fd = os.open(paths.active_pointer, os.O_RDONLY)
    else:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
    original_close = os.close
    original_open = os.open
    original_fstat = os.fstat
    closes = []
    recycled = []
    source = paths.active_pointer

    def close_once(descriptor) -> Never:
        closes.append(descriptor)
        original_close(descriptor)
        recycled.append(original_open(source, os.O_RDONLY))
        raise OSError(errno.EIO, "close failed after releasing descriptor")

    monkeypatch.setattr(database.os, "open", Mock(return_value=read_fd))
    monkeypatch.setattr(database.os, "close", close_once)
    try:
        with pytest.raises(OSError, match="after releasing"):
            with database._open_regular_file(tmp_path / "swapped") as opened:
                assert (opened is not None) is regular
        assert closes == [read_fd]
        assert recycled == [read_fd]
        assert original_fstat(recycled[0])
    finally:
        for descriptor in recycled:
            original_close(descriptor)


@pytest.mark.parametrize("after_raw", [False, True])
def test_failed_stream_construction_keeps_descriptor_ownership(
    catalog_config, monkeypatch, tmp_path, after_raw
) -> None:
    path = database.CatalogPaths.from_config(catalog_config).active_pointer
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"unrelated file")
    original_close = os.close
    original_open = os.open
    closes = Mock(wraps=original_close)
    owned = []
    other = []
    failure = MemoryError("stream construction failed")

    def fdopen(descriptor, mode, *, buffering=-1, closefd=True) -> Never:
        del buffering
        owned.append(descriptor)
        if after_raw:
            raw = io.FileIO(descriptor, mode, closefd=closefd)
            raw.close()  # Model io.open cleaning up after buffer construction fails.
        other.append(original_open(unrelated, os.O_RDONLY))
        raise failure

    monkeypatch.setattr(database.os, "fdopen", fdopen)
    monkeypatch.setattr(database.os, "close", closes)
    try:
        with pytest.raises(MemoryError) as raised:
            with database._open_regular_file(path):
                pytest.fail("failed construction must not enter the context")
        assert raised.value is failure
        closes.assert_called_once_with(owned[0])
        with pytest.raises(OSError):
            os.fstat(owned[0])
        assert os.fstat(other[0]).st_size == len(b"unrelated file")
    finally:
        for descriptor in other:
            with suppress(OSError):
                original_close(descriptor)


@pytest.mark.parametrize("nofollow", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_open_failure_probe_respects_nofollow(
    catalog_config, monkeypatch, nofollow, available
) -> None:
    path = database.CatalogPaths.from_config(catalog_config).active_pointer
    target = path.with_name("pointer-target.json")
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    error = PermissionError(errno.EACCES, "opening denied")
    monkeypatch.setattr(database.os, "O_NOFOLLOW", 1 if available else 0, raising=False)
    monkeypatch.setattr(database.os, "open", Mock(side_effect=error))
    if nofollow and available:
        with database._open_regular_file(path, nofollow=True) as opened:
            assert opened is None
    else:
        with pytest.raises(PermissionError) as raised:
            with database._open_regular_file(path):
                pytest.fail("a regular target's access denial must propagate")
        assert raised.value is error


@pytest.mark.parametrize("initial_failure", [False, True])
def test_regular_stream_cleanup_preserves_body_failure(
    catalog_config, monkeypatch, initial_failure
) -> None:
    path = database.CatalogPaths.from_config(catalog_config).active_pointer
    original_fdopen = os.fdopen
    handles = []
    body_error = RuntimeError("original body failure")
    close_error = OSError(errno.EIO, "cleanup failed")

    class Handle:
        def __init__(self, handle) -> None:
            self.handle = handle
            self.descriptor = handle.fileno()
            self.closes = 0

        def close(self) -> None:
            self.closes += 1
            self.handle.close()
            raise close_error

    def fdopen(*args: Any, **kwargs: Any) -> Handle:
        handle = Handle(original_fdopen(*args, **kwargs))
        handles.append(handle)
        return handle

    monkeypatch.setattr(database.os, "fdopen", fdopen)

    def exercise() -> None:
        with database._open_regular_file(path):
            if initial_failure:
                raise body_error

    with pytest.raises(RuntimeError if initial_failure else OSError) as raised:
        exercise()
    assert raised.value is (body_error if initial_failure else close_error)
    assert handles[0].closes == 1
    with pytest.raises(OSError):
        os.fstat(handles[0].descriptor)


@pytest.mark.parametrize("nonregular", [True, False])
def test_pointer_open_denial_requires_structural_evidence(
    catalog_config, monkeypatch, nonregular
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    if nonregular:
        paths.active_pointer.unlink()
        paths.active_pointer.mkdir()
    error = PermissionError(errno.EACCES, "pointer denied")
    monkeypatch.setattr(database.os, "open", Mock(side_effect=error))
    result = database._read_active_pointer(paths)
    assert result.snapshot_id == ""
    assert result.error is (None if nonregular else error)


@pytest.mark.parametrize("archive", [True, False])
def test_secondary_operational_probe_is_preserved(
    catalog_config, monkeypatch, tmp_path, archive
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    source = (
        paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
        if archive
        else paths.active_pointer
    )
    primary = PermissionError(errno.EACCES, "opening denied")
    secondary = OSError(errno.EMFILE, "probe exhausted descriptors")
    original_stat = Path.stat
    opened = False

    def open_denied(*_args: Any) -> Never:
        nonlocal opened
        opened = True
        raise primary

    def stat(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if opened and path == source:
            raise secondary
        return original_stat(path, *args, **kwargs)

    if archive:
        previous = next(
            item
            for item in asyncio.run(database.catalog_state(catalog_config)).files
            if item.dataset == "parts"
        )
    monkeypatch.setattr(database.os, "open", open_denied)
    monkeypatch.setattr(Path, "stat", stat)
    if archive:
        with pytest.raises(OSError) as raised:
            module._reuse_archive(
                paths,
                "fixture-snapshot",
                "parts.csv.gz",
                tmp_path / "copy.gz",
                previous,
            )
        assert raised.value is primary
    else:
        assert database._read_active_pointer(paths).error is primary
    assert any(str(secondary) in note for note in primary.__notes__)


def test_deep_pointer_json_releases_descriptor(catalog_config, monkeypatch) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    paths.active_pointer.write_text("[" * 10_000 + "0" + "]" * 10_000)
    original_open = os.open
    descriptors = []

    def record(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(database.os, "open", record)
    for _ in range(3):
        assert database._read_active_pointer(paths).snapshot_id == ""
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
