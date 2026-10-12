"""Shared local, pull-request, and release quality gates."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["uv", "run", "--locked", *args], cwd=ROOT, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "gate",
        choices=("static", "types", "tests", "performance", "generated", "workflows"),
    )
    parser.add_argument("--report-directory", type=Path, default=ROOT)
    parser.add_argument("--profile", choices=("ci", "extended"), default="ci")
    args = parser.parse_args()
    match args.gate:
        case "static":
            run("ruff", "format", "--check", "src", "scripts", "tests")
            run("ruff", "check", "src", "scripts", "tests")
            run("deptry", ".")
            run("pyroma", "--min", "10", ".")
        case "types":
            run("ty", "check", "src/rebrickable", "tests/typing")
            run(
                "pyrefly",
                "check",
                "--min-severity",
                "warn",
                "src/rebrickable",
                "tests/typing",
            )
        case "tests":
            args.report_directory.mkdir(parents=True, exist_ok=True)
            report = args.report_directory / "coverage.json"
            environment = {
                **os.environ,
                "PYTHONASYNCIODEBUG": "1",
                "HYPOTHESIS_PROFILE": args.profile,
            }
            run(
                "python",
                "-m",
                "pytest",
                "--asyncio-debug",
                "-m",
                "not integration and not mutation and not performance",
                "--cov=rebrickable",
                "--cov-branch",
                "--cov-report=term",
                f"--cov-report=xml:{args.report_directory / 'coverage.xml'}",
                f"--cov-report=json:{report}",
                env=environment,
            )
            run("python", "scripts/check_coverage.py", str(report))
        case "performance":
            run("python", "-m", "scripts.synthetic_catalog")
        case "generated":
            run("python", "scripts/check_generated.py")
        case "workflows":
            for executable, flag, version in (
                ("actionlint", "-version", "1.7.12"),
                ("zizmor", "--version", "1.30.1"),
                ("shellcheck", "--version", "0.11.0"),
            ):
                output = subprocess.check_output([executable, flag], text=True)
                if version not in output.split():
                    raise ValueError(f"{executable} must be version {version}")
            subprocess.run(
                ["actionlint", "-shellcheck=shellcheck"], cwd=ROOT, check=True
            )
            subprocess.run(["zizmor", "--offline", ".github"], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
