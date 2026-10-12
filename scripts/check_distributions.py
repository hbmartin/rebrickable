"""Verify distribution contents and exercise installed wheels outside the checkout."""

from __future__ import annotations

import argparse
import email
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(directory: Path) -> Path:
    (wheel,) = directory.glob("*.whl")
    (sdist,) = directory.glob("*.tar.gz")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        (metadata_name,) = (
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        wheel_metadata = email.message_from_bytes(archive.read(metadata_name))
        required = ["rebrickable/__init__.py", "rebrickable/py.typed"]
        for name in required:
            if name not in names:
                raise ValueError(f"wheel lacks {name}")
        licenses = [name for name in names if name.endswith("/licenses/LICENSE.txt")]
        if (
            len(licenses) != 1
            or archive.read(licenses[0]) != (ROOT / "LICENSE.txt").read_bytes()
        ):
            raise ValueError("wheel license is missing or differs from project")
        if not any(
            name.startswith("rebrickable/data/rebrickable-openapi-")
            and name.endswith(".json")
            for name in names
        ):
            raise ValueError("wheel lacks vendored OpenAPI resource")
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        for suffix in (
            "/LICENSE.txt",
            "/docs/api.md",
            "/tests/test_catalog_session.py",
            "/scripts/generate_openapi.py",
        ):
            if not any(name.endswith(suffix) for name in names):
                raise ValueError(f"sdist lacks {suffix}")
        (metadata_name,) = (name for name in names if name.endswith("/PKG-INFO"))
        handle = archive.extractfile(metadata_name)
        if handle is None:
            raise ValueError("sdist metadata is unreadable")
        with handle:
            sdist_metadata = email.message_from_bytes(handle.read())
    for field in (
        "Name",
        "Version",
        "Requires-Python",
        "License-Expression",
        "Requires-Dist",
        "Provides-Extra",
    ):
        if wheel_metadata.get_all(field) != sdist_metadata.get_all(field):
            raise ValueError(f"wheel/sdist metadata differs: {field}")
    for field, key in (
        ("Name", "name"),
        ("Version", "version"),
        ("Requires-Python", "requires-python"),
        ("License-Expression", "license"),
    ):
        if wheel_metadata[field] != project[key]:
            raise ValueError(f"distribution metadata differs from project: {field}")
    if (tag := os.environ.get("RELEASE_TAG")) and tag != f"v{project['version']}":
        raise ValueError("release tag does not match distribution version")

    def compact(requirement: str) -> str:
        normalized = requirement.replace(" ", "").lower()
        # Compare constraint sets independently of metadata serializer ordering.
        match = re.match(r"([a-z0-9_.-]+)(.*)", normalized)
        if match is None:
            raise ValueError("invalid requirement metadata")
        return match[1] + ",".join(sorted(match[2].split(",")))

    actual = {
        compact(item)
        for item in wheel_metadata.get_all("Requires-Dist", [])
        if ";" not in item
    }
    if actual != {compact(item) for item in project["dependencies"]}:
        raise ValueError("runtime dependencies differ from project")
    extras = project.get("optional-dependencies", {})
    if set(wheel_metadata.get_all("Provides-Extra", [])) != set(extras):
        raise ValueError("distribution extras differ from project")
    for extra, requirements in extras.items():
        actual_extra = set()
        for requirement in wheel_metadata.get_all("Requires-Dist", []):
            value, separator, marker = requirement.partition(";")
            if separator and marker.strip().replace('"', "'") == f"extra == '{extra}'":
                actual_extra.add(compact(value))
        if actual_extra != {compact(item) for item in requirements}:
            raise ValueError(f"extra dependencies differ from project: {extra}")
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    (root_package,) = (
        item for item in locked["package"] if item["name"] == project["name"]
    )
    if root_package["version"] != project["version"]:
        raise ValueError("lockfile project version differs from distribution")
    print(f"Verified {wheel.name} and {sdist.name}")
    return wheel.resolve()


def installed(wheel: Path, *, extra: bool, minimum: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="rebrickable-installed-") as directory:
        temporary = Path(directory)
        environment = temporary / "environment"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(environment)], check=True
        )
        python = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if minimum:
            requirements = temporary / "runtime.txt"
            command = [
                "uv",
                "pip",
                "compile",
                str(ROOT / "pyproject.toml"),
                "--python",
                str(python),
                "--resolution",
                "lowest-direct",
                "--output-file",
                str(requirements),
            ]
            if extra:
                command.extend(["--extra", "ldraw"])
            subprocess.run(command, check=True)
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "-r",
                    str(requirements),
                ],
                check=True,
            )
        requirement = str(wheel) + ("[ldraw]" if extra else "")
        command = ["uv", "pip", "install", "--python", str(python), requirement]
        if minimum:
            command.append("--no-deps")
        subprocess.run(command, check=True)
        subprocess.run(["uv", "pip", "check", "--python", str(python)], check=True)
        working = temporary / "working"
        working.mkdir()
        for name in ("installed_check.py", "catalog_fixture.py"):
            shutil.copy2(ROOT / "tests" / name, working / name)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        env["PYTHONASYNCIODEBUG"] = "1"
        subprocess.run(
            [
                str(python),
                str(working / "installed_check.py"),
                "ldraw" if extra else "core",
            ],
            cwd=working,
            env=env,
            check=True,
        )


def rebuild(directory: Path) -> Path:
    (sdist,) = directory.glob("*.tar.gz")
    with tempfile.TemporaryDirectory(prefix="rebrickable-sdist-") as temporary:
        unpacked = Path(temporary)
        with tarfile.open(sdist) as archive:
            archive.extractall(unpacked, filter="data")
        (source,) = unpacked.iterdir()
        output = directory / "rebuilt"
        subprocess.run(
            ["uv", "build", "--wheel", str(source), "--out-dir", str(output.resolve())],
            check=True,
        )
    (rebuilt,) = output.glob("*.whl")
    (original,) = directory.glob("*.whl")
    with zipfile.ZipFile(original) as left, zipfile.ZipFile(rebuilt) as right:
        if set(left.namelist()) != set(right.namelist()) or any(
            left.read(name) != right.read(name) for name in left.namelist()
        ):
            raise ValueError("sdist-built wheel contents differ from original wheel")
    installed(rebuilt.resolve(), extra=False)
    installed(rebuilt.resolve(), extra=True)
    return rebuilt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=ROOT / "dist")
    parser.add_argument(
        "--mode",
        choices=("verify", "core", "ldraw", "minimum", "rebuild"),
        default="verify",
    )
    args = parser.parse_args()
    wheel = verify(args.directory)
    match args.mode:
        case "core" | "ldraw":
            installed(wheel, extra=args.mode == "ldraw")
        case "minimum":
            installed(wheel, extra=False, minimum=True)
            installed(wheel, extra=True, minimum=True)
        case "rebuild":
            rebuild(args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
