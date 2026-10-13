from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rebrickable import cli
from rebrickable.errors import ApiError, EntityNotFoundError

ROOT = Path(__file__).resolve().parents[1]
STARTUP = """\
import sys
import pytest_socket
from rebrickable import cli
pytest_socket.disable_socket(allow_unix_socket=True)
"""


def closed_pipe(
    code: str, *, stream: str, unbuffered: bool = False
) -> subprocess.CompletedProcess[bytes]:
    reader, writer = os.pipe()
    os.close(reader)
    command = [sys.executable, *(["-u"] if unbuffered else []), "-c", STARTUP + code]
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            stdout=writer if stream == "stdout" else subprocess.PIPE,
            stderr=writer if stream == "stderr" else subprocess.PIPE,
            timeout=10,
            check=False,
        )
    finally:
        os.close(writer)


@pytest.mark.parametrize("unbuffered", [False, True])
@pytest.mark.parametrize(
    "argv",
    [
        ["url", "part", "3001"],
        ["--help"],
        ["--version"],
        ["catalog", "--help"],
        ["api-spec"],
    ],
)
def test_closed_stdout_exits_quietly(argv, unbuffered) -> None:
    result = closed_pipe(
        f"raise SystemExit(cli._entrypoint({argv!r}))",
        stream="stdout",
        unbuffered=unbuffered,
    )
    assert result.returncode == 0
    assert result.stderr == b""


@pytest.mark.parametrize("unbuffered", [False, True])
def test_usage_error_with_closed_stderr_preserves_status(unbuffered) -> None:
    result = closed_pipe(
        "raise SystemExit(cli._entrypoint(['unknown-command']))",
        stream="stderr",
        unbuffered=unbuffered,
    )
    assert result.returncode == 2
    assert result.stdout == b""


@pytest.mark.parametrize("status", [0, 3, 4, 5])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_closed_stderr_preserves_output_and_command_status(status, unbuffered) -> None:
    result = closed_pipe(
        f"""\
async def run(args):
    cli._print_stdout('healthy stdout')
    cli._print_stderr('lost diagnostic')
    cli._print_stderr('another lost diagnostic')
    return {status}
cli._run = run
raise SystemExit(cli._entrypoint(['url', 'part', '3001']))
""",
        stream="stderr",
        unbuffered=unbuffered,
    )
    assert result.returncode == status
    assert result.stdout == b"healthy stdout\n"


@pytest.mark.parametrize("status", [3, 4, 5])
def test_stdout_final_flush_preserves_computed_status(status) -> None:
    result = closed_pipe(
        f"""\
async def run(args):
    cli._print_stdout('buffered output')
    return {status}
cli._run = run
raise SystemExit(cli._entrypoint(['url', 'part', '3001']))
""",
        stream="stdout",
    )
    assert result.returncode == status
    assert result.stderr == b""


@pytest.mark.parametrize("failure", ["open", "dup2"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_failed_redirection_discards_pending_buffers(
    failure, stream, unbuffered
) -> None:
    result = closed_pipe(
        f"""\
import errno
import os
from types import SimpleNamespace
def failed(*args):
    raise OSError(errno.EMFILE, 'descriptor exhaustion')
cli.os = SimpleNamespace(
    devnull=os.devnull, O_WRONLY=os.O_WRONLY,
    open=failed if {failure!r} == 'open' else os.open,
    dup2=failed if {failure!r} == 'dup2' else os.dup2,
    close=os.close,
)
async def run(args):
    cli._print_stdout('healthy stdout')
    cli._print_stderr('lost diagnostic')
    return 0
cli._run = run
raise SystemExit(cli._entrypoint(['url', 'part', '3001']))
""",
        stream=stream,
        unbuffered=unbuffered,
    )
    assert result.returncode == 0
    if stream == "stdout":
        # Buffered output completes the command before finalization discovers EPIPE.
        assert result.stderr in (b"", b"lost diagnostic\n")
    else:
        assert result.stdout == b"healthy stdout\n"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ValueError("invalid input"), 2),
        (EntityNotFoundError("part", "missing"), 3),
        (ApiError(500, "/path", "op", "API failed"), 5),
        (BrokenPipeError("not from an output helper"), 1),
    ],
)
def test_error_diagnostics_with_broken_stderr_preserve_status(
    monkeypatch, capsys, error, status
) -> None:
    async def fail(_args: object) -> None:
        raise error

    stderr = Mock(spec=io.StringIO)
    stderr.closed = False
    stderr.write.side_effect = BrokenPipeError
    stderr.fileno.side_effect = io.UnsupportedOperation
    stderr.close.side_effect = ValueError
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=sys.stdout, stderr=stderr))
    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(["url", "part", "3001"]) == status
    assert capsys.readouterr() == ("", "")
    stderr.close.assert_not_called()


@pytest.mark.parametrize("absent", [True, False])
def test_unavailable_streams_do_not_crash(monkeypatch, capsys, absent) -> None:
    stream = None if absent else io.StringIO()
    if stream is not None:
        stream.close()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))
    assert cli.main(["url", "part", "3001"]) == 0
    with pytest.raises(SystemExit) as error:
        cli.main(["unknown-command"])
    assert error.value.code == 2
    assert capsys.readouterr() == ("", "")


def test_borrowed_descriptorless_stream_remains_open() -> None:
    stream = io.StringIO("buffered data")
    cli._silence_stream(stream)
    assert not stream.closed


def test_borrowed_descriptorless_buffer_remains_open() -> None:
    stream = io.TextIOWrapper(io.BytesIO())
    stream.write("pending output")
    cli._silence_stream(stream)
    assert not stream.closed


@pytest.mark.parametrize("status", [0, 3, 4, 5])
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_final_flush_handles_only_failed_stream(
    monkeypatch, status, stream_name
) -> None:
    async def run(_args: object) -> int:
        return status

    stdout = Mock(spec=io.StringIO)
    stderr = Mock(spec=io.StringIO)
    stdout.closed = stderr.closed = False
    failed = stdout if stream_name == "stdout" else stderr
    healthy = stderr if stream_name == "stdout" else stdout
    failed.flush.side_effect = BrokenPipeError
    failed.fileno.side_effect = io.UnsupportedOperation
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))
    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == status
    failed.close.assert_not_called()
    healthy.close.assert_not_called()
    healthy.flush.assert_called_once_with()


def test_argparse_output_to_explicit_stream() -> None:
    stream = io.StringIO()
    parser = cli._parser()
    parser.print_help(file=stream)
    assert "usage: rebrickable" in stream.getvalue()
    parser._print_message("", stream)
    with pytest.raises(SystemExit) as error:
        parser.exit()
    assert error.value.code == 0


def test_argparse_default_message_uses_stderr(capsys) -> None:
    cli._parser()._print_message("diagnostic\n")
    assert capsys.readouterr() == ("", "diagnostic\n")


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["catalog", "--help"]])
def test_argparse_exits_preserve_normal_output_and_status(capsys, argv) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 0
    capture = capsys.readouterr()
    assert "rebrickable" in capture.out
    assert capture.err == ""
