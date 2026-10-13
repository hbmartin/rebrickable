from __future__ import annotations

import errno
import io
from types import SimpleNamespace
from typing import override

import pytest

from rebrickable import cli


class RecordingRaw(io.RawIOBase):
    def __init__(self, error: OSError | None, *, fail_after: int = 0) -> None:
        super().__init__()
        self.error = error
        self.fail_after = fail_after
        self.writes = 0
        self.data = bytearray()

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, data, /) -> int:
        self.writes += 1
        if self.error is not None and self.writes > self.fail_after:
            raise self.error
        self.data.extend(data)
        return len(data)


@pytest.mark.parametrize("status", range(6))
@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENOSPC, "full"),
        OSError(errno.EINVAL, "invalid"),
        BrokenPipeError(errno.EPIPE, "pipe"),
    ],
)
def test_shared_diagnostic_flush_detects_pending_stdout_failure(
    monkeypatch, status, error
) -> None:
    raw = RecordingRaw(error)
    stream = io.TextIOWrapper(
        io.BufferedWriter(raw), encoding="ascii", line_buffering=True
    )
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stdout("pending result", status=status)
        cli._write_stderr("diagnostic\n")
        return status

    monkeypatch.setattr(cli, "_run", run)
    try:
        assert cli.main(["url", "part", "3001"]) == (
            status if isinstance(error, BrokenPipeError) else 1
        )
        assert raw.data == b""
        assert raw.writes == 1
        assert not stream.closed
    finally:
        raw.error = None
        stream.close()


@pytest.mark.parametrize("status", range(6))
@pytest.mark.parametrize("line_buffered", [False, True])
def test_shared_diagnostic_failure_after_stdout_flush_preserves_status(
    monkeypatch, status, line_buffered
) -> None:
    raw = RecordingRaw(OSError(errno.ENOSPC, "diagnostics full"), fail_after=1)
    stream = io.TextIOWrapper(
        io.BufferedWriter(raw), encoding="ascii", line_buffering=line_buffered
    )
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stdout("delivered result", status=status)
        cli._write_stderr("diagnostic\n")
        return status

    monkeypatch.setattr(cli, "_run", run)
    try:
        assert cli.main(["url", "part", "3001"]) == status
        assert raw.data == b"delivered result"
        assert raw.writes == 2
        assert not stream.closed
    finally:
        raw.error = None
        stream.close()


def test_shared_encoding_suppression_and_records_are_invocation_local(
    monkeypatch,
) -> None:
    raw = RecordingRaw(None)
    stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="ascii")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stdout("prefix", status=4)
        cli._write_stderr("\N{SNOWMAN}")
        cli._write_stdout("tail", status=4)
        cli._write_stdout("end", status=4)
        state = cli._output_state.get()
        assert len(state.streams) == 1
        assert state.streams[0].pending_stdout
        return 4

    monkeypatch.setattr(cli, "_run", run)
    try:
        for _ in range(2):
            assert cli.main(["url", "part", "3001"]) == 4
        assert raw.data == b"prefixtailend" * 2
        assert not stream.closed
    finally:
        stream.close()
