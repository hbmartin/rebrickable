from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.check_coverage import check_coverage


def report(
    path: Path, total: object = 100, covered: object = 95, *, branch: bool = True
) -> Path:
    path.write_text(
        json.dumps(
            {
                "meta": {"branch_coverage": branch},
                "totals": {"num_branches": total, "covered_branches": covered},
            }
        )
    )
    return path


def test_independent_branch_floor(tmp_path: Path) -> None:
    path = tmp_path / "coverage.json"
    assert "95/100" in check_coverage(report(path))
    with pytest.raises(ValueError, match="below"):
        check_coverage(report(path, 10000, 9499))
    assert "minimum 94" in check_coverage(path, Decimal(94))


@pytest.mark.parametrize(
    ("total", "covered"),
    [
        (0, 0),
        (-1, 0),
        (100, -1),
        (100, 101),
        (True, 1),
        (100, False),
        (100, 95.0),
        (None, None),
    ],
)
def test_invalid_branch_totals(tmp_path: Path, total: object, covered: object) -> None:
    with pytest.raises(ValueError):
        check_coverage(report(tmp_path / "report.json", total, covered))


@pytest.mark.parametrize(
    "payload",
    ["{", "[]", "{}", '{"meta":{},"totals":{}}', '{"meta":{"branch_coverage":true}}'],
)
def test_missing_malformed_report(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "report.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        check_coverage(path)
    with pytest.raises(FileNotFoundError):
        check_coverage(tmp_path / "absent.json")


@pytest.mark.parametrize("threshold", ["NaN", "Infinity", "-1", "101"])
def test_invalid_threshold(tmp_path: Path, threshold: str) -> None:
    with pytest.raises(ValueError, match="threshold"):
        check_coverage(report(tmp_path / "coverage.json"), Decimal(threshold))


def test_branch_measurement_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not measure"):
        check_coverage(report(tmp_path / "coverage.json", branch=False))


def test_coverage_cli_failure_and_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.check_coverage import main  # noqa: PLC0415 - CLI entry point

    monkeypatch.setattr(
        "sys.argv", ["check", str(report(tmp_path / "coverage.json", 100, 95))]
    )
    assert main() == 0
    assert "95/100" in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.argv", ["check", str(report(tmp_path / "coverage.json", 100, 94))]
    )
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 1
    assert "below 95" in capsys.readouterr().err


@pytest.mark.parametrize("existing", [False, True])
def test_drift_dispatch_success(
    monkeypatch: pytest.MonkeyPatch, *, existing: bool
) -> None:
    from scripts import dispatch_drift  # noqa: PLC0415 - patch target

    calls: list[tuple[str, ...]] = []

    def command(*args: str) -> str:
        calls.append(args)
        if args[1:3] == ("diff", "--cached"):
            return "fixture patch"
        if args[1] == "ls-remote":
            return "existing branch" if existing else ""
        if args[1] == "rev-parse":
            return "a" * 40
        if args[1:3] == ("pr", "list"):
            return '[{"url":"https://example.test/pr/1"}]' if existing else "[]"
        return ""

    monkeypatch.setattr(dispatch_drift, "command", command)
    dispatch_drift.publish_drift("b" * 40, "automation/fixture")
    assert any(call[1:3] == ("pr", "create") for call in calls) is not existing
    dispatches = [call for call in calls if call[1:3] == ("workflow", "run")]
    assert [call[3] for call in dispatches] == ["lint-test.yml", "security.yml"]
    assert all(call[-1] == f"expected_sha={'a' * 40}" for call in dispatches)


@pytest.mark.parametrize(
    "stage",
    [
        ("gh", "auth"),
        ("git", "push"),
        ("git", "ls-remote"),
        ("gh", "pr", "list"),
        ("gh", "pr", "create"),
        ("gh", "workflow", "run", "lint-test.yml"),
        ("gh", "workflow", "run", "security.yml"),
    ],
)
def test_drift_command_failures_propagate(
    monkeypatch: pytest.MonkeyPatch, stage: tuple[str, ...]
) -> None:
    import subprocess  # noqa: PLC0415 - failure type

    from scripts import dispatch_drift  # noqa: PLC0415 - patch target

    def command(*args: str) -> str:
        if args[: len(stage)] == stage:
            raise subprocess.CalledProcessError(1, args)
        if args[1:3] == ("diff", "--cached"):
            return "fixture patch"
        if args[1:3] == ("pr", "list"):
            return "[]"
        return ""

    monkeypatch.setattr(dispatch_drift, "command", command)
    with pytest.raises(subprocess.CalledProcessError):
        dispatch_drift.publish_drift("a" * 40, "automation/fixture")


def test_revision_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import check_revision  # noqa: PLC0415 - patch target

    monkeypatch.setattr(
        check_revision.subprocess, "check_output", lambda *_args, **_kwargs: "a" * 40
    )
    assert check_revision.check_revision("a" * 40) == "a" * 40
    for expected in ("b" * 40, "main", "a" * 39):
        with pytest.raises(ValueError, match="expected revision"):
            check_revision.check_revision(expected)


def test_coverage_large_counts_do_not_round_at_threshold(tmp_path: Path) -> None:
    total = 10**60
    covered = total * 95 // 100 - 1
    with pytest.raises(ValueError, match="below"):
        check_coverage(report(tmp_path / "large.json", total, covered))


def test_drift_no_changes_and_invalid_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import dispatch_drift  # noqa: PLC0415 - patch target

    calls: list[tuple[str, ...]] = []

    def empty(*args: str) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(dispatch_drift, "command", empty)
    dispatch_drift.publish_drift("a" * 40, "automation/fixture")
    assert not any(args[1] == "push" for args in calls)

    def invalid(*args: str) -> str:
        if args[1:3] == ("diff", "--cached"):
            return "fixture patch"
        if args[1:3] == ("pr", "list"):
            return "{}"
        return ""

    monkeypatch.setattr(dispatch_drift, "command", invalid)
    with pytest.raises(ValueError, match="lookup"):
        dispatch_drift.publish_drift("a" * 40, "automation/fixture")
