"""Verify formatted OpenAPI regeneration without changing tracked files."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rebrickable.data import OPENAPI_RESOURCE

ROOT = Path(__file__).resolve().parents[1]


def check_generated() -> None:
    with tempfile.TemporaryDirectory(prefix="rebrickable-generated-") as directory:
        temporary = Path(directory)
        registry = temporary / "operation_registry.py"
        queries = temporary / "query_types.py"
        original = ROOT / "src/rebrickable/api"
        shutil.copy2(original / registry.name, registry)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/generate_openapi.py"),
                "--input",
                str(ROOT / "src/rebrickable/data" / OPENAPI_RESOURCE),
                "--registry",
                str(registry),
                "--query-types",
                str(queries),
            ],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "ruff",
                "format",
                "--config",
                str(ROOT / "pyproject.toml"),
                str(registry),
                str(queries),
            ],
            cwd=ROOT,
            check=True,
        )
        for generated in (registry, queries):
            if generated.read_bytes() != (original / generated.name).read_bytes():
                raise ValueError(f"generated artifact differs: {generated.name}")
    print("Generated artifacts are byte-identical")


if __name__ == "__main__":
    check_generated()
