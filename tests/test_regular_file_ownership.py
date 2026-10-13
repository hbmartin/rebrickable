from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from typing import Any, Never
from unittest.mock import Mock

import pytest

from rebrickable import refresh as module
from rebrickable.catalog import database


def test_nonregular_close_does_not_close_recycled_descriptor(
    catalog_config, monkeypatch, tmp_path
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
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
                assert opened is None
        assert closes == [read_fd]
        assert recycled == [read_fd]
        assert original_fstat(recycled[0])
    finally:
        for descriptor in recycled:
            original_close(descriptor)


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
