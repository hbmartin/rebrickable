"""Vendor a dated upstream Swagger document and regenerate reviewed artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.generate_openapi import HTTP_METHODS, operations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    raw = args.input.read_bytes()
    document: dict[str, Any] = json.loads(raw)
    if document.get("swagger") != "2.0" or not isinstance(document.get("paths"), dict):
        raise SystemExit("upstream document is not the expected Swagger 2.0 shape")
    current_module = root / "src/rebrickable/data/__init__.py"
    resource_line = next(
        line
        for line in current_module.read_text(encoding="utf-8").splitlines()
        if line.startswith("OPENAPI_RESOURCE")
    )
    current_name = resource_line.partition("=")[2].strip().strip('"')
    current = root / "src/rebrickable/data" / current_name
    if (
        current.is_file()
        and hashlib.sha256(current.read_bytes()).digest()
        == hashlib.sha256(raw).digest()
    ):
        return 0
    destination_name = f"rebrickable-openapi-{args.date}.json"
    destination = root / "src/rebrickable/data" / destination_name
    destination.write_bytes(raw)
    current_module.write_text(
        '"""Vendored reproducibility data."""\n\n'
        f'OPENAPI_RESOURCE = "{destination_name}"\n\n'
        '__all__ = ["OPENAPI_RESOURCE"]\n',
        encoding="utf-8",
    )
    items = operations(document)
    if len(items) == 63:
        subprocess.run(
            [
                "uv",
                "run",
                "python",
                "scripts/generate_openapi.py",
                "--input",
                str(destination),
                "--registry",
                "src/rebrickable/api/operation_registry.py",
                "--models",
                "src/rebrickable/api/generated_requests.py",
                "--allow-new-checksum",
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(
            [
                "uv",
                "run",
                "ruff",
                "format",
                "src/rebrickable/api/operation_registry.py",
                "src/rebrickable/api/generated_requests.py",
            ],
            cwd=root,
            check=True,
        )
    else:
        count = sum(
            method in HTTP_METHODS
            for item in document["paths"].values()
            for method in item
        )
        print(f"vendored drift with {count} operations; review is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
