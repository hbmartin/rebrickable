from __future__ import annotations

import errno
import io
from contextlib import suppress
from types import SimpleNamespace
from typing import override
from unittest.mock import Mock

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


@pytest.mark.parametrize("status", range(6))
def test_distinct_write_and_flush_failures_are_each_reported(
    monkeypatch, status
) -> None:
    raw = RecordingRaw(OSError(errno.ENOSPC, "buffer flush full"))
    stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="ascii")
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stderr))

    async def run(_args) -> int:
        cli._write_stdout("buffered", status=status)
        cli._write_stdout("\N{SNOWMAN}", status=status)
        return status

    monkeypatch.setattr(cli, "_run", run)
    try:
        assert cli.main(["url", "part", "3001"]) == 1
        lines = stderr.getvalue().splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("unable to write stdout: ")
        assert "codec can't encode" in lines[0]
        assert lines[1] == "unable to flush stdout: [Errno 28] buffer flush full"
        assert not stream.closed
    finally:
        raw.error = None
        stream.close()


def test_suppressed_write_failure_is_not_labeled_flush(monkeypatch) -> None:
    stream = Mock(spec=io.StringIO, closed=False)
    stream.write.side_effect = OSError(errno.ENOSPC, "write full")
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stderr))

    async def run(_args) -> int:
        with suppress(OSError):
            cli._write_stdout("result")
        return 0

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == 1
    assert stderr.getvalue() == "unable to write stdout: [Errno 28] write full\n"
    stream.flush.assert_not_called()


@pytest.mark.parametrize("status", range(6))
def test_aliased_final_diagnostic_reaches_raw_stream(monkeypatch, status) -> None:
    raw = RecordingRaw(None)
    stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="ascii")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stdout("buffered result", status=status)
        with suppress(OSError):
            cli._write_stdout("\N{SNOWMAN}", status=status)
        return status

    monkeypatch.setattr(cli, "_run", run)
    try:
        for _ in range(2):
            assert cli.main(["url", "part", "3001"]) == 1
        assert raw.data.count(b"buffered result") == 2
        assert raw.data.count(b"unable to write stdout:") == 2
        assert not stream.closed
        stream.write("host still works")
        stream.flush()
        assert raw.data.endswith(b"host still works")
    finally:
        stream.close()


@pytest.mark.parametrize("arguments", [["--help"], ["--version"], ["not-a-command"]])
def test_parser_output_failure_reports_once_without_changing_exit(
    monkeypatch, arguments
) -> None:
    raw = RecordingRaw(OSError(errno.ENOSPC, "parser output full"))
    stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="ascii")
    stderr = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stderr", stderr)
    if arguments == ["not-a-command"]:
        # Usage errors are written to stderr and retain status 2 if it fails.
        monkeypatch.setattr(cli.sys, "stderr", stream)
    try:
        with pytest.raises(SystemExit) as raised:
            cli.main(arguments)
        assert raised.value.code == (2 if arguments == ["not-a-command"] else 0)
        if arguments != ["not-a-command"]:
            assert stderr.getvalue().count("unable to flush stdout:") == 1
        assert not stream.closed
    finally:
        raw.error = None
        stream.close()
