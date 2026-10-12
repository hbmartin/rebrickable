from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from rebrickable import Config, RebrickableSession
from scripts import benchmark_catalog, install_workflow_tools, measure_process


@pytest.mark.parametrize("rss", [123, 513])
async def test_benchmark_uses_five_warmed_medians_and_checks_rss(
    catalog_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rss: int,
) -> None:
    samples = iter([99.0, 0.001, 0.002, 100.0, 0.003, 0.004])
    calls: list[float] = []

    async def sample(_args: argparse.Namespace) -> dict[str, float]:
        value = next(samples)
        calls.append(value)
        return dict.fromkeys(
            (
                "connect_seconds",
                "warm_part_seconds",
                "warm_search_seconds",
                "validation_seconds",
                "validation_seconds_per_row",
            ),
            value,
        )

    connect = RebrickableSession.connect

    async def fixture_session() -> RebrickableSession:
        return await connect(catalog_config)

    monkeypatch.setattr(benchmark_catalog, "sample", sample)
    monkeypatch.setattr(benchmark_catalog, "peak_rss_mib", lambda: rss)
    monkeypatch.setattr(RebrickableSession, "connect", fixture_session)
    args = argparse.Namespace(
        max_connect_seconds=5,
        max_part_seconds=0.05,
        max_search_seconds=1,
        max_validation_seconds_per_row=0.03,
        max_rss_mib=512,
    )
    if rss > 512:
        with pytest.raises(RuntimeError, match="RSS"):
            await benchmark_catalog.benchmark(args)
    else:
        result = await benchmark_catalog.benchmark(args)
        assert result["medians"]["connect_seconds"] == 0.003
    report = json.loads(capsys.readouterr().out)
    assert len(calls) == 6
    assert len(report["samples"]) == 5
    assert report["snapshot"]["schema_version"] == 1
    assert report["snapshot"]["files"]


@pytest.mark.parametrize(
    ("memory", "exit_code", "expected"),
    [(1024, 0, 0), (1024, 7, 7), (524289, 0, None), (None, 0, None)],
)
def test_process_memory_gate_and_command_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory: int | None,
    exit_code: int,
    expected: int | None,
) -> None:
    report = tmp_path / "memory.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "measure",
            "--report",
            str(report),
            "--stdout",
            str(tmp_path / "output.json"),
            "--",
            "fixture",
        ],
    )

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        Path(args[3]).write_text(
            f"Maximum resident set size (kbytes): {memory}"
            if memory is not None
            else "missing data"
        )
        return subprocess.CompletedProcess(args, exit_code)

    monkeypatch.setattr(measure_process.subprocess, "run", run)
    if expected is None:
        with pytest.raises((ValueError, RuntimeError), match="RSS"):
            measure_process.main()
    else:
        assert measure_process.main() == expected
        assert json.loads(report.read_text())["peak_rss_mib"] == 1


@pytest.mark.parametrize("valid", [False, True])
def test_tool_installer_checks_digest_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, valid: bool
) -> None:
    contents = b"fixture executable"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("nested/fixture")
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest() if valid else "0" * 64
    monkeypatch.setattr(install_workflow_tools.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_workflow_tools.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        install_workflow_tools,
        "TOOLS",
        {"fixture": ("https://example.test/binary", digest)},
    )
    monkeypatch.setattr(
        install_workflow_tools.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(payload),
    )
    monkeypatch.delenv("GITHUB_PATH", raising=False)
    if valid:
        install_workflow_tools.install(tmp_path)
        assert (tmp_path / "fixture").read_bytes() == contents
    else:
        with pytest.raises(ValueError, match="checksum"):
            install_workflow_tools.install(tmp_path)
        assert not (tmp_path / "fixture").exists()
