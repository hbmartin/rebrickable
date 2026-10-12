"""Offline checks of advisory workflow reporting and cache isolation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOW = yaml.safe_load(
    Path(__file__)
    .resolve()
    .parents[1]
    .joinpath(".github/workflows/garuff.yml")
    .read_text()
)
_JOB = _WORKFLOW["jobs"]["garuff"]
_CHECK = next(step for step in _JOB["steps"] if "run" in step)


def test_garuff_cache_is_separate_and_job_remains_advisory() -> None:
    setup = next(
        step
        for step in _JOB["steps"]
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    )
    assert setup["with"]["cache-suffix"] == "garuff"
    assert _JOB["continue-on-error"] is True
    upload = _JOB["steps"][-1]
    assert upload["if"] == "always()"


@pytest.mark.parametrize(
    ("status", "output"),
    [
        (0, "No findings\n"),
        (1, "example.py:1:1: GAC009 lint finding\n"),
        (1, "Traceback (most recent call last):\nRuntimeError: checker crashed\n"),
        (2, "Invalid checker configuration\n"),
    ],
    ids=["success", "findings", "crash", "configuration"],
)
def test_garuff_reporting_preserves_status_and_output(
    tmp_path: Path, status: int, output: str
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "uv"
    stub.write_text('#!/bin/sh\nprintf "%s" "$STUB_OUTPUT"\nexit "$STUB_STATUS"\n')
    stub.chmod(0o755)
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _CHECK["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            **_CHECK["env"],
            "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(summary),
            "STUB_STATUS": str(status),
            "STUB_OUTPUT": output,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == status
    report = tmp_path / "garuff-report"
    assert report.joinpath("check.txt").read_text() == output
    assert report.joinpath("exit-code.txt").read_text() == f"{status}\n"
    assert output in result.stdout
    contents = summary.read_text()
    assert f"Exit code: {status}." in contents
    if status == 0:
        assert "No findings." in contents
        assert "::warning::" not in result.stdout
    else:
        assert "Findings, parse/suppression errors, or tool failure" in contents
        assert "::warning::garuff is advisory" in result.stdout
