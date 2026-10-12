from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from rebrickable import Config, RebrickableClient
from rebrickable.api.transport import HttpxTransport

from .http_fixtures import ByteStream, encode_body


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "gzip, deflate", "zstd"])
async def test_api_decodes_compressed_responses_and_closes_stream(
    monkeypatch, encoding: str
) -> None:
    payload = {"part_num": "3001", "name": "Brick 2 x 4", "custom": "extra" * 1000}
    body = encode_body(json.dumps(payload).encode(), encoding)
    stream = ByteStream((body[:3], body[3:]))

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "key test-key"
        return httpx2.Response(
            200,
            headers={"content-encoding": encoding, "content-type": "application/json"},
            stream=stream,
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(
        "rebrickable.api.transport.httpx2.AsyncClient", lambda **_: client
    )
    async with RebrickableClient(
        api_key="test-key", config=Config(request_interval=0)
    ) as api:
        part = await api.get_part("3001")
        assert part.part_num == payload["part_num"]
        assert part.name == payload["name"]
        assert part.extra["custom"] == payload["custom"]
    assert stream.closed
    assert client.is_closed


@pytest.mark.parametrize("failure", ["decoding", "read"])
async def test_api_transport_failures_propagate_and_close_stream(
    monkeypatch, failure
) -> None:
    stream = ByteStream(
        (b"invalid gzip",) if failure == "decoding" else (),
        error=httpx2.ReadError("interrupted response") if failure == "read" else None,
    )

    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(
        "rebrickable.api.transport.httpx2.AsyncClient", lambda **_: client
    )
    transport = HttpxTransport(timeout=httpx2.Timeout(1))
    try:
        with pytest.raises(
            httpx2.DecodingError if failure == "decoding" else httpx2.ReadError
        ):
            await transport.request(
                "GET", "https://example.test/parts/3001/", headers={}
            )
    finally:
        await transport.close()
    assert stream.closed
    assert client.is_closed


async def test_api_cancelled_response_closes_stream(monkeypatch) -> None:
    started = asyncio.Event()
    stream = ByteStream((b"{}",), started=started, resume=asyncio.Event())

    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=stream)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(
        "rebrickable.api.transport.httpx2.AsyncClient", lambda **_: client
    )
    transport = HttpxTransport(timeout=httpx2.Timeout(1))
    try:
        task = asyncio.create_task(
            transport.request("GET", "https://example.test/parts/3001/", headers={})
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await transport.close()
    assert stream.closed
    assert client.is_closed
