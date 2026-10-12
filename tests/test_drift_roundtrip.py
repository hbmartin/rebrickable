from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import dispatch_drift


def test_repeated_drift_recreates_branch_and_preserves_patch_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.chdir(repository)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("config", "core.autocrlf", "false")
    data = repository / "src/rebrickable/data"
    api = repository / "src/rebrickable/api"
    data.mkdir(parents=True)
    api.mkdir()
    registry = api / "operation_registry.py"
    registry.write_bytes(b"old registry\r\n")
    (api / "query_types.py").write_bytes(b"old queries\r\n")
    (data / "old.json").write_bytes(b'{"old":true}\r\n')
    git("add", ".")
    git("commit", "-m", "Fixture base")
    base = git("rev-parse", "HEAD")
    published: list[str] = []
    dispatches: list[tuple[str, ...]] = []
    original_command = dispatch_drift.command
    branch = "automation/openapi-fixture"

    def command(*args: str) -> str:
        if args[0] == "gh":
            if args[1:3] == ("pr", "list"):
                return '[{"url":"https://example.test/pr/1"}]' if published else "[]"
            if args[1:3] == ("workflow", "run"):
                dispatches.append(args)
            return ""
        if args[1] == "ls-remote":
            return published[-1] if published else ""
        if args[1] == "fetch":
            return git("update-ref", f"refs/remotes/origin/{branch}", published[-1])
        if args[1] == "push":
            assert args[2:] == ("--force-with-lease", "origin", branch)
            published.append(git("rev-parse", "HEAD"))
            return ""
        return original_command(*args)

    monkeypatch.setattr(dispatch_drift, "command", command)
    for iteration in (1, 2):
        # Each workflow run starts at main and generates a different daily diff.
        git("switch", "main")
        registry_bytes = f"registry generation {iteration}\r\n".encode()
        archive_bytes = f'{{"generation":{iteration}}}\r\n'.encode()
        registry.write_bytes(registry_bytes)
        (data / "old.json").unlink()
        generated = data / "dated.json"
        generated.write_bytes(archive_bytes)
        dispatch_drift.publish_drift(base, branch)
        assert registry.read_bytes() == registry_bytes
        assert generated.read_bytes() == archive_bytes
        assert not (data / "old.json").exists()
        assert git("rev-parse", "HEAD^") == base
        assert git("status", "--porcelain") == ""
        assert dispatches[-1][-1] == f"expected_sha={published[-1]}"
    assert len(published) == 2
    assert published[0] != published[1]
    assert len(dispatches) == 4
