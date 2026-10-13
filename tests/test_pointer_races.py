from __future__ import annotations

import errno
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from rebrickable import refresh as module
from rebrickable.catalog import database
from rebrickable.errors import CatalogImportError
from tests.test_refresh_recovery import stable_sources
from tests.test_refresh_state import downloader


def test_missing_root_terminates_with_original_cause(
    catalog_config, monkeypatch
) -> None:
    paths = replace(
        database.CatalogPaths.from_config(catalog_config),
        active_pointer=Path("/missing-volume/active.json"),
    )
    original_stat = Path.stat
    error = FileNotFoundError(errno.ENOENT, "missing root")
    attempts = []

    def stat(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if kwargs.get("follow_symlinks", True):
            attempts.append(path)
            if len(attempts) > 4:
                raise AssertionError("root traversal did not terminate")
            raise error
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(CatalogImportError) as exc:
        module._check_pointer_destination(paths)
    assert exc.value.__cause__ is error
    assert attempts == [Path("/missing-volume"), Path("/")]


@pytest.mark.parametrize("swap", ["directory", "missing", "symlink", "regular"])
def test_pointer_reads_one_regular_descriptor(
    catalog_config, monkeypatch, swap
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer
    payload = pointer.read_bytes()
    original_open = os.open
    original_fdopen = os.fdopen
    fdopen = Mock(wraps=original_fdopen)
    monkeypatch.setattr(database.os, "fdopen", fdopen)

    def open_after_swap(path, flags) -> int:
        pointer.unlink()
        if swap == "directory":
            pointer.mkdir()
        elif swap == "symlink":
            target = pointer.with_name("target.json")
            target.write_bytes(payload)
            pointer.symlink_to(target.name)
        elif swap == "regular":
            pointer.write_bytes(payload)
        return original_open(path, flags)

    monkeypatch.setattr(database.os, "open", open_after_swap)
    result = database._read_active_pointer(paths)
    assert result.snapshot_id == (
        "fixture-snapshot"
        if swap in {"regular", "symlink"}
        else (None if swap == "missing" else "")
    )
    assert fdopen.call_count == int(swap in {"regular", "symlink"})


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_pointer_fifo_swap_cannot_block(catalog_config) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
from pathlib import Path
from unittest.mock import patch
from rebrickable.catalog.database import CatalogPaths, _read_active_pointer
from rebrickable.config import Config
paths = CatalogPaths.from_config(Config(database_path=Path(sys.argv[1])))
original_open = os.open
opened = []
def swap(path, flags):
    assert flags & os.O_NONBLOCK
    path.unlink()
    os.mkfifo(path)
    descriptor = original_open(path, flags)
    opened.append(descriptor)
    return descriptor
with patch.object(os, 'open', swap):
    assert _read_active_pointer(paths).snapshot_id == ''
try:
    os.fstat(opened[0])
except OSError:
    pass
else:
    raise AssertionError('pointer descriptor leaked')
""",
            str(catalog_config.database_path),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""


@pytest.mark.parametrize("stage", ["carryover", "move"])
async def test_late_pointer_obstruction_prevents_installation(
    catalog_config, monkeypatch, stage
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    monkeypatch.setattr(
        module, "_download_one", downloader(stable_sources(catalog_config))
    )
    original = module._carry_crosswalk if stage == "carryover" else module.shutil.move

    def obstruct(*args: Any, **kwargs: Any) -> str | None:
        result = original(*args, **kwargs)
        paths.active_pointer.unlink()
        paths.active_pointer.mkdir()
        return result

    monkeypatch.setattr(
        module if stage == "carryover" else module.shutil,
        "_carry_crosswalk" if stage == "carryover" else "move",
        obstruct,
    )
    with pytest.raises(CatalogImportError, match="unable to activate"):
        await module.refresh_catalog(catalog_config)
    assert {child.name for child in paths.snapshots_dir.iterdir()} == {
        "fixture-snapshot"
    }
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("stage", ["fstat", "fdopen"])
def test_pointer_descriptor_released_after_operational_failure(
    catalog_config, monkeypatch, stage
) -> None:
    paths = database.CatalogPaths.from_config(catalog_config)
    error = OSError(errno.EMFILE, "resources exhausted")
    close = Mock(wraps=os.close)
    monkeypatch.setattr(database.os, "close", close)
    monkeypatch.setattr(database.os, stage, Mock(side_effect=error))
    pointer = database._read_active_pointer(paths)
    assert pointer.error is error
    close.assert_called_once()
