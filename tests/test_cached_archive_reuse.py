from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any
from unittest.mock import Mock

import httpx2
import pytest

from rebrickable import CatalogStatus, RefreshOutcome
from rebrickable import refresh as module
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.errors import DownloadError
from tests.test_refresh_recovery import stable_sources


@pytest.mark.parametrize("probe_error", [errno.ENOENT, errno.EACCES])
def test_denied_archive_open_probes_without_hiding_operational_errors(
    catalog_config, tmp_path, monkeypatch, probe_error
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    error = PermissionError(errno.EACCES, "open denied")
    original_stat = Path.stat
    denied = False

    def open_source(_path, _flags) -> int:
        nonlocal denied
        denied = True
        raise error

    def stat_source(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if denied and path == source:
            raise OSError(probe_error, "probe failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", open_source)
    monkeypatch.setattr(Path, "stat", stat_source)
    if probe_error == errno.ENOENT:
        assert not module._reuse_archive(
            paths, "fixture-snapshot", source.name, tmp_path / "saved", previous
        )
    else:
        with pytest.raises(PermissionError) as raised:
            module._reuse_archive(
                paths, "fixture-snapshot", source.name, tmp_path / "saved", previous
            )
        assert raised.value is error


@pytest.fixture(autouse=True)
def conditional_archives(catalog_config) -> None:
    manifest = CatalogPaths.from_config(catalog_config).manifest_for("fixture-snapshot")
    payload = json.loads(manifest.read_text())
    for item in payload["files"]:
        item["etag"] = '"cached"'
        item["last_modified"] = "Sat, 01 Aug 2026 00:00:00 GMT"
    manifest.write_text(json.dumps(payload))


class ArchiveStream(httpx2.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), 31):
            yield self.body[start : start + 31]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "missing",
        "directory",
        "directory-swap",
        "directory-denied",
        "outside",
        "contained",
        "mismatch",
        "loop",
    ],
)
async def test_streaming_304_safe_reuse_or_one_fresh_download(
    catalog_config, tmp_path, monkeypatch, damage
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    sources = stable_sources(catalog_config)
    expected = sources["parts"].read_bytes()
    cached = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    outside = tmp_path / "outside.gz"
    outside.write_bytes(b"outside must not be read")
    if damage in {"missing", "directory", "outside", "contained", "loop"}:
        cached.unlink()
    if damage == "directory":
        cached.mkdir()
    elif damage == "outside":
        cached.symlink_to(outside)
    elif damage == "contained":
        target = cached.with_name("contained.gz")
        target.write_bytes(expected)
        cached.symlink_to(target.name)
    elif damage == "loop":
        cached.symlink_to(cached.name)
    elif damage == "mismatch":
        cached.write_bytes(b"different archive bytes")
    elif damage in {"directory-swap", "directory-denied"}:
        original_open = os.open

        def swap(path, flags, *args: Any, **kwargs: Any) -> int:
            if path == cached:
                cached.unlink()
                cached.mkdir()
                if damage == "directory-denied":
                    raise PermissionError(errno.EACCES, "directory open denied")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", swap)
    requests = []
    streams = []

    def respond(request) -> httpx2.Response:
        requests.append(request)
        if "if-none-match" in request.headers:
            body, status = b"", 304
        else:
            body = sources[
                Path(request.url.path).name.removesuffix(".csv.gz")
            ].read_bytes()
            status = 200
        stream = ArchiveStream(body)
        streams.append(stream)
        return httpx2.Response(status, stream=stream, headers={"etag": '"fresh"'})

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        module.httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(respond), **kwargs),
    )
    report = await module.refresh_catalog(catalog_config)
    fallback = damage not in {"none", "contained"}
    assert report.outcome is (
        RefreshOutcome.UPDATED if fallback else RefreshOutcome.UNCHANGED
    )
    part_requests = [
        request for request in requests if request.url.path.endswith("/parts.csv.gz")
    ]
    assert len(part_requests) == (2 if fallback else 1)
    for request in requests:
        assert request.headers["accept-encoding"] == "identity"
    first = part_requests[0]
    assert "if-none-match" in first.headers
    if fallback:
        assert "if-none-match" not in part_requests[1].headers
        assert "if-modified-since" not in part_requests[1].headers
    saved = paths.snapshots_dir / report.snapshot_id / "parts.csv.gz"
    assert saved.read_bytes() == expected
    fingerprint = next(item for item in report.files if item.dataset == "parts")
    assert fingerprint.byte_length == len(expected)
    assert fingerprint.sha256 == hashlib.sha256(expected).hexdigest()
    assert all(stream.closed for stream in streams)
    assert outside.read_bytes() == b"outside must not be read"
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("fresh_status", [304, 503])
async def test_failed_fallback_preserves_previous_snapshot(
    catalog_config, monkeypatch, fresh_status
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    (paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz").unlink()
    requests = []

    def respond(request) -> httpx2.Response:
        requests.append(request)
        status = 304 if "if-none-match" in request.headers else fresh_status
        return httpx2.Response(status, stream=ArchiveStream(b""))

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        module.httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(respond), **kwargs),
    )
    with pytest.raises(DownloadError):
        await module.refresh_catalog(catalog_config)
    assert (
        len(
            [
                request
                for request in requests
                if request.url.path.endswith("/parts.csv.gz")
            ]
        )
        == 2
    )
    assert paths.active_pointer.read_bytes() == pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("swap", ["file", "outside-link", "directory-link"])
def test_check_open_race_never_reads_swapped_file(
    catalog_config, tmp_path, monkeypatch, swap
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    replacement = outside / source.name
    replacement.write_bytes(b"outside")
    original_open = os.open

    def open_after_swap(path, flags) -> int:
        if swap == "directory-link":
            source.parent.rename(source.parent.with_name("moved"))
            source.parent.symlink_to(outside, target_is_directory=True)
        else:
            source.unlink()
            if swap == "outside-link":
                source.symlink_to(replacement)
            else:
                source.write_bytes(b"changed inode")
        return original_open(path, flags)

    monkeypatch.setattr(module.os, "open", open_after_swap)
    destination = tmp_path / "saved.gz"
    assert not module._reuse_archive(
        paths, "fixture-snapshot", source.name, destination, previous
    )
    assert not destination.exists()
    assert replacement.read_bytes() == b"outside"


@pytest.mark.parametrize("where", ["source", "permission", "destination"])
async def test_operational_reuse_errors_do_not_redownload(
    catalog_config, monkeypatch, where
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_bytes()
    original_open = Path.open
    error = OSError(
        {
            "source": errno.EMFILE,
            "permission": errno.EACCES,
            "destination": errno.ENOSPC,
        }[where],
        "reuse operational failure",
    )
    if where != "destination":
        original_descriptor_open = os.open

        def open_source(path, *args: Any, **kwargs: Any) -> int:
            if Path(path).name == "parts.csv.gz":
                raise error
            return original_descriptor_open(path, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", open_source)
    else:

        def open_destination(path, *args: Any, **kwargs: Any) -> IO[Any]:
            if path.name == "parts.csv.gz" and path.parent.name.startswith("refresh-"):
                raise error
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_destination)
    requests = []

    def respond(request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(304, stream=ArchiveStream(b""))

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        module.httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(respond), **kwargs),
    )
    with pytest.raises(DownloadError, match="reuse operational failure"):
        await module.refresh_catalog(catalog_config)
    assert all("if-none-match" in request.headers for request in requests)
    assert paths.active_pointer.read_bytes() == original_pointer
    assert (
        await catalog_state(catalog_config, verify=True)
    ).status is CatalogStatus.READY
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ELOOP, errno.EMFILE])
def test_reuse_rechecks_identity_and_releases_descriptor(
    catalog_config, tmp_path, monkeypatch, error_number
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    resolved_source = source.resolve()
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    original_stat = Path.stat
    original_open = os.open
    descriptors = []

    def opened(path, flags) -> int:
        descriptor = original_open(path, flags)
        descriptors.append(descriptor)
        return descriptor

    def stat_after_open(path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == resolved_source and descriptors:
            raise OSError(error_number, "cached identity changed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", opened)
    monkeypatch.setattr(Path, "stat", stat_after_open)
    destination = tmp_path / "reuse.gz"
    if error_number == errno.EMFILE:
        with pytest.raises(OSError, match="cached identity changed"):
            module._reuse_archive(
                paths, "fixture-snapshot", source.name, destination, previous
            )
    else:
        assert not module._reuse_archive(
            paths, "fixture-snapshot", source.name, destination, previous
        )
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert descriptors
    assert not destination.exists()


def test_reuse_releases_descriptor_when_file_wrapper_fails(
    catalog_config, tmp_path, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    descriptors = []

    def fail_wrapper(descriptor, _mode, *, closefd) -> IO[bytes]:
        assert not closefd
        descriptors.append(descriptor)
        raise OSError(errno.EMFILE, "file wrapper failed")

    monkeypatch.setattr(module.os, "fdopen", fail_wrapper)
    with pytest.raises(OSError, match="file wrapper failed"):
        module._reuse_archive(
            paths, "fixture-snapshot", "parts.csv.gz", tmp_path / "saved.gz", previous
        )
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("changed", ["st_size", "st_mtime_ns", "st_ctime_ns"])
def test_reused_inode_with_changed_metadata_is_rejected_before_reading(
    catalog_config, tmp_path, monkeypatch, changed
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    before = source.stat()
    metadata = {
        name: getattr(before, name)
        for name in (
            "st_mode",
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    }
    metadata[changed] += 1
    monkeypatch.setattr(
        module.os, "fstat", lambda _descriptor: SimpleNamespace(**metadata)
    )
    destination = tmp_path / "reuse.gz"
    assert not module._reuse_archive(
        paths, "fixture-snapshot", source.name, destination, previous
    )
    assert not destination.exists()


def test_reuse_rejects_length_mismatch_before_open(
    catalog_config, tmp_path, monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    previous = next(
        item
        for item in asyncio.run(catalog_state(catalog_config)).files
        if item.dataset == "parts"
    )
    source = paths.snapshots_dir / "fixture-snapshot" / "parts.csv.gz"
    source.write_bytes(b"wrong size")
    opened = Mock()
    monkeypatch.setattr(module.os, "open", opened)
    assert not module._reuse_archive(
        paths, "fixture-snapshot", source.name, tmp_path / "saved.gz", previous
    )
    opened.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_reuse_fifo_swap_completes_without_reading(catalog_config, tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, os, sys, threading
from pathlib import Path
import rebrickable.refresh as module
from rebrickable.config import Config
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable._workers import run_in_thread
config = Config(database_path=Path(sys.argv[1]))
paths = CatalogPaths.from_config(config)
previous = next(item for item in asyncio.run(catalog_state(config)).files if item.dataset == 'parts')
original = os.open
descriptors = []
opened, release = threading.Event(), threading.Event()
def swap(path, flags):
    Path(path).unlink()
    os.mkfifo(path)
    descriptor = original(path, flags)
    descriptors.append(descriptor)
    opened.set()
    assert release.wait(5)
    return descriptor
module.os.open = swap
destination = Path(sys.argv[2])
async def reuse():
    task = asyncio.create_task(run_in_thread(module._reuse_archive, paths, 'fixture-snapshot', 'parts.csv.gz', destination, previous))
    assert await asyncio.to_thread(opened.wait, 3)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    assert not task.done()
    release.set()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('cancellation was lost')
asyncio.run(reuse())
assert not destination.exists()
try:
    os.fstat(descriptors[0])
except OSError:
    pass
else:
    raise AssertionError('descriptor leaked')
""",
            str(catalog_config.database_path),
            str(tmp_path / "saved.gz"),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stderr == b""
