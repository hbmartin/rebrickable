"""Push a reviewed drift branch and explicitly request validation with GITHUB_TOKEN."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True)


def publish_drift(base_sha: str, branch: str) -> None:
    command("gh", "auth", "setup-git")
    command(
        "git",
        "add",
        "-A",
        "src/rebrickable/data",
        "src/rebrickable/api/operation_registry.py",
        "src/rebrickable/api/query_types.py",
    )
    patch = subprocess.check_output(["git", "diff", "--cached", "--binary"])
    if not patch:
        print("No upstream API drift")
        return
    with tempfile.TemporaryDirectory(prefix="openapi-drift-") as directory:
        patch_file = Path(directory) / "openapi.patch"
        patch_file.write_bytes(patch)
        command("git", "reset")
        existing = command("git", "ls-remote", "--heads", "origin", branch)
        if existing:
            command("git", "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}")
        command("git", "switch", "-f", "-C", branch, base_sha)
        command("git", "clean", "-fd", "src/rebrickable/data")
        command("git", "apply", "--index", str(patch_file))
    command("git", "config", "user.name", "github-actions")
    command("git", "config", "user.email", "github-actions@github.com")
    command("git", "commit", "-m", "chore: vendor upstream API drift")
    command("git", "push", "--force-with-lease", "origin", branch)
    sha = command("git", "rev-parse", "HEAD").strip()
    prs = json.loads(
        command(
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            "main",
            "--head",
            branch,
            "--json",
            "url",
        )
    )
    if not isinstance(prs, list):
        raise ValueError("existing-PR lookup returned invalid data")  # noqa: TRY004 - external command data
    if not prs:
        command(
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            "chore: review Rebrickable API drift",
            "--body",
            "Automated dated Swagger update. Parity CI intentionally remains failing for unreviewed endpoints or signature changes.",
        )
    for workflow in ("lint-test.yml", "security.yml"):
        command(
            "gh",
            "workflow",
            "run",
            workflow,
            "--ref",
            branch,
            "--field",
            f"expected_sha={sha}",
        )
    print(f"Dispatched validation and security for {sha}")


if __name__ == "__main__":
    publish_drift(
        command("git", "rev-parse", "HEAD").strip(),
        f"automation/openapi-{datetime.now(UTC):%Y-%m-%d}",
    )
