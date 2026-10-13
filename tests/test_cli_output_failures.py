from __future__ import annotations

import errno
import io
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rebrickable import cli
from rebrickable.types import ExitCode
from tests.test_cli_pipes import STARTUP, closed_pipe


@pytest.mark.parametrize(
    "command",
    ["status", "path", "doctor", "bom", "ldraw-json", "ldraw-csv", "ldraw-table"],
)
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("unbuffered", [False, True])
def test_real_command_status_survives_closed_stdout(
    tmp_path, command, large, unbuffered
) -> None:
    code = f"""
from dataclasses import replace
from pathlib import Path
from tests.catalog_fixture import build_catalog_config
from tests.test_cli_config_search_more import translation_report
config = build_catalog_config(Path({str(tmp_path)!r}))
cli.Config.load = lambda: config
payload = 'x' * {20000 if large else 1}
command = {command!r}
if command in {{'status', 'path'}}:
    from rebrickable.catalog.database import CatalogPaths
    CatalogPaths.from_config(config).active_pointer.unlink()
original_state = cli.RebrickableSession.state
async def state(self, *, verify=False):
    value = await original_state(self, verify=verify)
    return replace(value, database_path=Path(payload), diagnostics=(payload,))
cli.RebrickableSession.state = state
if command == 'doctor':
    cli._package_version = lambda name: 'conflict'
if command == 'bom':
    path = Path({str(tmp_path / "bom.csv")!r})
    path.write_text('Part,Color,Quantity\\n' + payload + ',4,1\\n')
    argv = ['--format', 'json', 'bom', 'validate', str(path)]
elif command.startswith('ldraw'):
    report = replace(translation_report(resolved=False), diagnostics=(payload,))
    if command in {"ldraw-csv", "ldraw-table"}:
        report = replace(report, rows=tuple(replace(row, ldraw_part_num=payload) for row in report.rows))
    async def translate(self, *args, **kwargs):
        return report
    from rebrickable.bridge.ldraw import LDrawBridge
    LDrawBridge.translate_model_path = translate
    argv = ['translate-ldraw', 'model.ldr']
    if command == 'ldraw-json': argv.append('--json')
    if command == 'ldraw-csv': argv.append('--csv')
elif command == 'status': argv = ['--format', 'json', 'status']
else: argv = ['--format', 'json', 'catalog', command]
raise SystemExit(cli._entrypoint(argv))
"""
    result = closed_pipe(code, stream="stdout", unbuffered=unbuffered)
    expected = 3 if command in {"status", "path"} else 2 if command == "doctor" else 4
    assert result.returncode == expected, result.stderr.decode()
    assert b"Traceback" not in result.stderr
    assert b"BrokenPipe" not in result.stderr


@pytest.mark.parametrize("status", range(6))
@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EINVAL])
def test_operational_stdout_errors_return_one_and_finalize_stderr(
    monkeypatch, status, stage, error_number
) -> None:
    stdout = Mock(spec=io.StringIO, closed=False)
    stderr = io.StringIO()
    getattr(stdout, stage).side_effect = OSError(error_number, "output failed")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    async def run(_args) -> int:
        cli._print_stdout("result", status=status)
        return status

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == 1
    assert "output failed" in stderr.getvalue()
    stdout.close.assert_not_called()
    assert not stderr.closed


@pytest.mark.parametrize(
    ("argv", "status"), [(["--help"], 0), (["--version"], 0), (["unknown"], 2)]
)
@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize(
    "error",
    [OSError(errno.ENOSPC, "full"), AttributeError("missing"), ValueError("closed")],
)
def test_parser_output_errors_preserve_system_exit(
    monkeypatch, argv, status, stage, error
) -> None:
    stdout = Mock(spec=io.StringIO, closed=False)
    stderr = Mock(spec=io.StringIO, closed=False)
    for stream in (stdout, stderr):
        getattr(stream, stage).side_effect = error
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == status
    for stream in (stdout, stderr):
        stream.close.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        AttributeError("write unavailable"),
        OSError(errno.EINVAL, "bad stream"),
        ValueError("closed"),
    ],
)
def test_parser_ignores_explicit_message_stream_errors(error) -> None:
    stream = Mock()
    stream.write.side_effect = error
    cli._parser().print_help(file=stream)


@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize("error", [AttributeError("missing"), ValueError("closed")])
def test_unusable_borrowed_stdout_is_operational_failure(
    monkeypatch, stage, error
) -> None:
    stdout = Mock(spec=io.StringIO, closed=False)
    stderr = Mock(spec=io.StringIO, closed=False)
    getattr(stdout, stage).side_effect = error
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    async def run(_args) -> int:
        cli._print_stdout("result", status=ExitCode.INCOMPLETE_TRANSLATION)
        return ExitCode.INCOMPLETE_TRANSLATION

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == 1
    stderr.flush.assert_called_once_with()
    stdout.close.assert_not_called()


@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize("status", range(6))
def test_stderr_operational_errors_preserve_status(monkeypatch, stage, status) -> None:
    stderr = Mock(spec=io.StringIO, closed=False)
    getattr(stderr, stage).side_effect = OSError(errno.ENOSPC, "full")
    stdout = io.StringIO()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    async def run(_args) -> int:
        cli._print_stdout("healthy")
        cli._print_stderr("diagnostic")
        cli._print_stderr("second diagnostic")
        return status

    monkeypatch.setattr(cli, "_run", run)
    for _ in range(2):
        assert cli.main(["url", "part", "3001"]) == status
    assert stdout.getvalue() == "healthy\nhealthy\n"
    assert getattr(stderr, stage).call_count == 2
    stderr.close.assert_not_called()


def test_borrowed_binary_stream_keeps_descriptor(monkeypatch, tmp_path) -> None:
    path = tmp_path / "owned-by-caller"
    with path.open("wb") as raw:
        stream = io.TextIOWrapper(raw)
        monkeypatch.setattr(
            cli, "sys", SimpleNamespace(stdout=stream, stderr=io.StringIO())
        )
        original_write = stream.write
        stream.write = Mock(side_effect=BrokenPipeError)
        silence = Mock(wraps=cli._silence_stream)
        monkeypatch.setattr(cli, "_silence_stream", silence)
        assert cli.main(["url", "part", "3001"]) == 0
        silence.assert_called_once_with(stream)
        stream.write = original_write
        assert not raw.closed
        stream.write("still writable")
        stream.flush()
        assert path.read_text() == "still writable"
        stream.detach()


@pytest.mark.parametrize("winerror", [109, 232, 233])
def test_explicit_windows_pipe_codes_preserve_status(monkeypatch, winerror) -> None:
    error = OSError(errno.EINVAL, "closed Windows pipe")
    error.winerror = winerror
    stream = Mock(spec=io.StringIO, closed=False)
    stream.write.side_effect = error
    monkeypatch.setattr(
        cli, "sys", SimpleNamespace(stdout=stream, stderr=io.StringIO())
    )

    async def run(_args) -> int:
        cli._print_stdout("result", status=ExitCode.INCOMPLETE_TRANSLATION)
        return ExitCode.INCOMPLETE_TRANSLATION

    monkeypatch.setattr(cli, "_run", run)
    status = cli.main(["url", "part", "3001"])
    assert type(status) is int
    assert status == 4


def test_original_stream_with_unexpected_descriptor_remains_untouched(
    monkeypatch,
) -> None:
    stream = Mock(spec=io.TextIOWrapper, closed=False)
    stream.fileno.return_value = 42
    monkeypatch.setattr(cli, "sys", SimpleNamespace(__stdout__=stream))
    token = cli._output_state.set(cli._OutputState(owned=((stream, 1),)))
    try:
        cli._silence_stream(stream)
    finally:
        cli._output_state.reset(token)
    stream.buffer.close.assert_not_called()
    stream.close.assert_not_called()


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="requires /dev/full")
@pytest.mark.parametrize(
    ("argv", "status"), [(["api-spec"], 1), (["--help"], 0), (["--version"], 0)]
)
@pytest.mark.parametrize("unbuffered", [False, True])
def test_real_full_device(argv, status, unbuffered) -> None:
    with Path("/dev/full").open("wb") as full:
        result = subprocess.run(
            [
                sys.executable,
                *(["-u"] if unbuffered else []),
                "-c",
                STARTUP + f"raise SystemExit(cli._entrypoint({argv!r}))",
            ],
            stdout=full,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    assert result.returncode == status
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("executable", [False, True])
def test_encoding_failure_preserves_buffer_and_host_output(executable) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            STARTUP
            + f"""
sys.stdout.reconfigure(encoding='ascii')
async def run(args):
    cli._write_stdout('buffered before error\\n')
    cli._write_stdout('\\u00e9\\n')
    return 0
cli._run = run
status = cli.{"_entrypoint" if executable else "main"}(['url', 'part', '3001'])
assert status == 1
print('host output after return')
""",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b"buffered before error\nhost output after return\n"
    assert b"unable to write stdout" in result.stderr
    assert b"Traceback" not in result.stderr


def test_embedded_original_streams_never_redirect_or_close(monkeypatch) -> None:
    stdout = Mock(spec=io.TextIOWrapper, closed=False)
    stdout.write.side_effect = OSError(errno.ENOSPC, "full")
    stderr = io.StringIO()
    monkeypatch.setattr(
        cli, "sys", SimpleNamespace(stdout=stdout, __stdout__=stdout, stderr=stderr)
    )
    assert cli.main(["url", "part", "3001"]) == 1
    assert cli._output_state.get() is None
    stdout.close.assert_not_called()
    stdout.fileno.assert_not_called()
    assert "full" in stderr.getvalue()


@pytest.mark.parametrize("descriptor_kind", ["pipe", "file", "unknown"])
def test_windows_errno_only_einval_uses_descriptor_type(
    monkeypatch, descriptor_kind
) -> None:
    stream = Mock(spec=io.TextIOWrapper, closed=False)
    stream.fileno.return_value = 1
    stream.write.side_effect = OSError(errno.EINVAL, "closed")
    fstat = Mock(
        return_value=SimpleNamespace(
            st_mode=stat.S_IFIFO if descriptor_kind == "pipe" else stat.S_IFREG
        )
    )
    if descriptor_kind == "unknown":
        fstat.side_effect = OSError(errno.EBADF, "unavailable")
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt", fstat=fstat))
    monkeypatch.setattr(
        cli, "sys", SimpleNamespace(stdout=stream, stderr=io.StringIO())
    )

    async def run(_args) -> int:
        cli._write_stdout("result", status=4)
        return 4

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == (4 if descriptor_kind == "pipe" else 1)


def test_encoding_failure_on_stderr_still_flushes_buffer(monkeypatch) -> None:
    buffer = io.BytesIO()
    stderr = io.TextIOWrapper(buffer, encoding="ascii")
    stdout = io.StringIO()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    async def run(_args) -> int:
        cli._write_stderr("buffered diagnostic\n")
        cli._write_stderr("\u00e9")
        cli._write_stderr("suppressed diagnostic")
        cli._write_stdout("healthy output")
        return 5

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == 5
    assert buffer.getvalue() == b"buffered diagnostic\n"
    assert stdout.getvalue() == "healthy output"
    stderr.detach()


@pytest.mark.parametrize("status", range(6))
def test_aliased_encoding_failure_only_suppresses_diagnostics(
    monkeypatch, status
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stderr("buffered diagnostic\n")
        cli._write_stderr("é")
        cli._write_stderr("suppressed diagnostic")
        cli._write_stdout("healthy ASCII output\n", status=status)
        return status

    monkeypatch.setattr(cli, "_run", run)
    for _ in range(2):
        assert cli.main(["url", "part", "3001"]) == status
    assert buffer.getvalue() == b"buffered diagnostic\nhealthy ASCII output\n" * 2
    stream.write("host still works")
    stream.flush()
    assert buffer.getvalue().endswith(b"host still works")
    stream.detach()


@pytest.mark.parametrize("status", range(6))
@pytest.mark.parametrize(
    "error",
    [BrokenPipeError(errno.EPIPE, "pipe closed"), OSError(errno.ENOSPC, "full")],
)
def test_aliased_physical_failure_keeps_its_classification(
    monkeypatch, status, error
) -> None:
    stream = Mock(spec=io.StringIO, closed=False)
    stream.write.side_effect = error
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        cli._write_stderr("diagnostic")
        cli._write_stdout("result", status=status)
        return status

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == (
        status if isinstance(error, BrokenPipeError) else 1
    )
    stream.write.assert_called_once_with("diagnostic")
    stream.close.assert_not_called()


def test_repeated_encoding_failure_does_not_neutralize_stream(monkeypatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(
        cli,
        "sys",
        SimpleNamespace(stdout=stream, __stdout__=stream, stderr=io.StringIO()),
    )
    silence = Mock(wraps=cli._silence_stream)
    monkeypatch.setattr(cli, "_silence_stream", silence)

    async def run(_args) -> int:
        cli._write_stdout("preserved buffer\n")
        with pytest.raises(OSError):
            cli._write_stdout("é")
        cli._write_stdout("suppressed output")
        return 0

    monkeypatch.setattr(cli, "_run", run)
    assert cli._entrypoint(["url", "part", "3001"]) == 1
    silence.assert_not_called()
    assert buffer.getvalue() == b"preserved buffer\n"
    stream.detach()


@pytest.mark.parametrize("status", range(6))
@pytest.mark.parametrize("stdout_first", [False, True])
def test_aliased_diagnostic_only_failure_preserves_status(
    monkeypatch, status, stdout_first
) -> None:
    stream = Mock(spec=io.StringIO, closed=False)
    stream.write.side_effect = ([None] if stdout_first else []) + [
        OSError(errno.ENOSPC, "diagnostics full")
    ]
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        if stdout_first:
            cli._write_stdout("result", status=status)
        cli._write_stderr("diagnostic")
        return status

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == status
    assert stream.flush.call_count == int(stdout_first)
    stream.close.assert_not_called()


@pytest.mark.parametrize("stdout_used", [False, True])
@pytest.mark.parametrize("status", range(6))
def test_shared_finalization_runs_once_and_attributes_new_failure(
    monkeypatch, stdout_used, status
) -> None:
    stream = Mock(spec=io.StringIO, closed=False)
    stream.flush.side_effect = OSError(errno.ENOSPC, "flush full")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stream, stderr=stream))

    async def run(_args) -> int:
        if stdout_used:
            cli._write_stdout("result", status=status)
        cli._write_stderr("diagnostic")
        return status

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == (1 if stdout_used else status)
    stream.flush.assert_called_once()
    stream.close.assert_not_called()


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_stdout_operational_failure_is_reported_once(monkeypatch, stage) -> None:
    stdout = Mock(spec=io.StringIO, closed=False)
    getattr(stdout, stage).side_effect = OSError(errno.ENOSPC, "one output failure")
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    async def run(_args) -> int:
        cli._write_stdout("result", status=4)
        return 4

    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["url", "part", "3001"]) == 1
    assert stderr.getvalue().count("one output failure") == 1
    if stage == "write":
        stdout.flush.assert_not_called()
