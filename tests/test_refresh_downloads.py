from __future__ import annotations

import asyncio
import gzip
import hashlib
import zlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from rebrickable import Config, RebrickableSession, RefreshOutcome
from rebrickable.catalog.database import CatalogPaths, catalog_state
from rebrickable.catalog.importers import DATASET_BY_NAME, inspect_header
from rebrickable.errors import CatalogImportError, DatasetSchemaError, DownloadError
from rebrickable.progress import ProgressEvent, ProgressStage
from rebrickable.refresh import _download_one, refresh_catalog

from .http_fixtures import ByteStream, encode_body


@pytest.fixture
def archive_bodies(catalog_config: Config) -> dict[str, bytes]:
    snapshot = (
        CatalogPaths.from_config(catalog_config).snapshots_dir / "fixture-snapshot"
    )
    return {path.name: path.read_bytes() for path in snapshot.glob("*.csv.gz")}


def serve_archives(
    monkeypatch: pytest.MonkeyPatch,
    bodies: dict[str, bytes],
    *,
    parts_stream: ByteStream | None = None,
) -> list[ByteStream]:
    streams: list[ByteStream] = []
    client_type = httpx2.AsyncClient

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["accept-encoding"] == "identity"
        name = Path(request.url.path).name
        stream = (
            parts_stream
            if name == "parts.csv.gz" and parts_stream is not None
            else ByteStream((bodies[name],))
        )
        streams.append(stream)
        return httpx2.Response(
            200,
            headers={"content-encoding": "gzip", "etag": '"fixture"'},
            stream=stream,
        )

    def client(**kwargs: Any) -> httpx2.AsyncClient:
        return client_type(transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr("rebrickable.refresh.httpx2.AsyncClient", client)
    return streams


@pytest.mark.parametrize(
    "encoding",
    [None, "identity", "gzip", "deflate", "gzip, deflate", "zstd", "unknown"],
)
async def test_download_preserves_importable_archive_and_fingerprint(
    tmp_path: Path, catalog_config: Config, archive_bodies: dict[str, bytes], encoding
) -> None:
    body = archive_bodies["parts.csv.gz"]
    stream = ByteStream((body[:7], body[7:]))
    previous = replace(
        (await catalog_state(catalog_config)).files[0],
        etag='"previous"',
        last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
    )

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["if-none-match"] == previous.etag
        assert request.headers["if-modified-since"] == previous.last_modified
        headers = {"etag": '"current"', "last-modified": previous.last_modified}
        if encoding is not None:
            headers["content-encoding"] = encoding
        return httpx2.Response(200, headers=headers, stream=stream)

    destination = tmp_path / "parts.csv.gz"
    events: list[ProgressEvent] = []
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        changed, fingerprint = await _download_one(
            client,
            url="https://example.test/parts.csv.gz",
            destination=destination,
            previous=previous,
            force=False,
            callback=events.append,
        )
    assert changed
    assert destination.read_bytes() == body
    assert inspect_header(destination, DATASET_BY_NAME["parts"]) == ("future_column",)
    assert fingerprint["byte_length"] == len(body)
    assert fingerprint["sha256"] == hashlib.sha256(body).hexdigest()
    assert fingerprint["etag"] == '"current"'
    assert fingerprint["last_modified"] == previous.last_modified
    assert [event.current for event in events] == [7, len(body)]
    assert all(event.stage is ProgressStage.DOWNLOAD for event in events)
    assert stream.closed


@pytest.mark.parametrize("force", [False, True])
async def test_304_and_force_preserve_conditional_request_behavior(
    tmp_path: Path, catalog_config: Config, *, force: bool
) -> None:
    previous = replace(
        (await catalog_state(catalog_config)).files[0],
        etag='"previous"',
        last_modified="Sat, 01 Aug 2026 00:00:00 GMT",
    )
    stream = ByteStream(())

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["accept-encoding"] == "identity"
        assert ("if-none-match" in request.headers) is not force
        assert ("if-modified-since" in request.headers) is not force
        return httpx2.Response(304, stream=stream)

    destination = tmp_path / "unused.csv.gz"
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        changed, fingerprint = await _download_one(
            client,
            url=previous.url,
            destination=destination,
            previous=previous,
            force=force,
            callback=None,
        )
    assert not changed
    assert fingerprint == asdict(previous)
    assert not destination.exists()
    assert stream.closed


async def test_real_streaming_refresh_promotes_readable_snapshot(
    catalog_config: Config, archive_bodies: dict[str, bytes], monkeypatch
) -> None:
    streams = serve_archives(monkeypatch, archive_bodies)
    report = await refresh_catalog(catalog_config)
    assert report.outcome is RefreshOutcome.UPDATED
    snapshot = (
        CatalogPaths.from_config(catalog_config).snapshots_dir / report.snapshot_id
    )
    for fingerprint in report.files:
        name = f"{fingerprint.dataset}.csv.gz"
        assert (snapshot / name).read_bytes() == archive_bodies[name]
        assert fingerprint.sha256 == hashlib.sha256(archive_bodies[name]).hexdigest()
        assert fingerprint.byte_length == len(archive_bodies[name])
    async with await RebrickableSession.connect(catalog_config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"
    assert len(streams) == 12
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize(
    "failure",
    [
        "not-gzip",
        "deflate",
        "truncated-header",
        "truncated-footer",
        "crc",
        "late-deflate",
        "read",
    ],
)
async def test_failed_download_or_import_preserves_snapshot(
    catalog_config: Config, archive_bodies: dict[str, bytes], monkeypatch, failure: str
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    original_pointer = paths.active_pointer.read_bytes()
    body = archive_bodies["parts.csv.gz"]
    malformed = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 20
    error_type = DatasetSchemaError
    stream = None
    if failure == "not-gzip":
        body = b"plain CSV"
    elif failure == "deflate":
        body = malformed
    elif failure == "truncated-header":
        body = body[:8]
    elif failure in {"truncated-footer", "late-deflate"}:
        payload = gzip.decompress(body) + b"".join(
            f"extra{i},Extra part,1,Plastic,ignored\n".encode() for i in range(1000)
        )
        body = gzip.compress(payload, mtime=0)
        body = body[:-8] if failure == "truncated-footer" else body + malformed
        error_type = CatalogImportError
    elif failure == "crc":
        body = body[:-8] + bytes([body[-8] ^ 255]) + body[-7:]
        error_type = CatalogImportError
    else:
        stream = ByteStream((body[:8],), error=httpx2.ReadError("interrupted transfer"))
        error_type = DownloadError
    archive_bodies["parts.csv.gz"] = body
    streams = serve_archives(monkeypatch, archive_bodies, parts_stream=stream)
    with pytest.raises(error_type) as failure_info:
        await refresh_catalog(catalog_config)
    if failure in {"deflate", "late-deflate"}:
        assert isinstance(failure_info.value.__cause__, zlib.error)
    assert paths.active_pointer.read_bytes() == original_pointer
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert not tuple(paths.snapshots_dir.glob(".incoming-*"))
    assert all(item.closed for item in streams)
    async with await RebrickableSession.connect(catalog_config) as session:
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "zstd"])
async def test_extra_transport_wrapping_fails_catalog_validation(
    catalog_config: Config, archive_bodies: dict[str, bytes], monkeypatch, encoding: str
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    archive_bodies["parts.csv.gz"] = encode_body(
        archive_bodies["parts.csv.gz"], encoding
    )
    streams = serve_archives(monkeypatch, archive_bodies)
    with pytest.raises(DatasetSchemaError):
        await refresh_catalog(catalog_config)
    assert paths.active_pointer.read_bytes() == pointer
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert all(stream.closed for stream in streams)


async def test_cancelled_raw_transfer_closes_response_and_preserves_snapshot(
    catalog_config: Config, archive_bodies: dict[str, bytes], monkeypatch
) -> None:
    paths = CatalogPaths.from_config(catalog_config)
    pointer = paths.active_pointer.read_bytes()
    started = asyncio.Event()
    stream = ByteStream((b"\x1f\x8b",), started=started, resume=asyncio.Event())
    streams = serve_archives(monkeypatch, archive_bodies, parts_stream=stream)
    task = asyncio.create_task(refresh_catalog(catalog_config))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert paths.active_pointer.read_bytes() == pointer
    assert not tuple(catalog_config.cache_path.glob("refresh-*"))
    assert all(item.closed for item in streams)
