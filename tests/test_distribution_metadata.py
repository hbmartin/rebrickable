from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import check_distributions


@pytest.fixture
def distribution_factory(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "LICENSE.txt").write_text("fixture license")
    (root / "pyproject.toml").write_text(
        """\
[project]
name = "rebrickable"
version = "1.1.0"
requires-python = ">=3.12"
license = "GPL-3.0-or-later"
dependencies = ["filelock>=3.20.3", "anyio>=4.14.2; sys_platform != 'emscripten'"]
[project.optional-dependencies]
ldraw = ["requests>=2.33.0"]
"""
    )
    (root / "uv.lock").write_text('[[package]]\nname="rebrickable"\nversion="1.1.0"\n')
    monkeypatch.setattr(check_distributions, "ROOT", root)
    monkeypatch.delenv("RELEASE_TAG", raising=False)
    directory = tmp_path / "dist"
    directory.mkdir()

    def build(requirements: list[str]) -> Path:
        metadata = "\n".join(
            [
                "Metadata-Version: 2.4",
                "Name: rebrickable",
                "Version: 1.1.0",
                "Requires-Python: >=3.12",
                "License-Expression: GPL-3.0-or-later",
                "Provides-Extra: ldraw",
                *(f"Requires-Dist: {value}" for value in requirements),
                "",
            ]
        ).encode()
        with zipfile.ZipFile(directory / "rebrickable.whl", "w") as archive:
            archive.writestr("rebrickable/__init__.py", "")
            archive.writestr("rebrickable/py.typed", "")
            archive.writestr("rebrickable/data/rebrickable-openapi-fixture.json", "{}")
            archive.writestr("rebrickable.dist-info/METADATA", metadata)
            archive.writestr(
                "rebrickable.dist-info/licenses/LICENSE.txt", "fixture license"
            )
        with tarfile.open(directory / "rebrickable.tar.gz", "w:gz") as archive:
            files = {
                "LICENSE.txt": b"fixture license",
                "docs/api.md": b"",
                "tests/test_catalog_session.py": b"",
                "scripts/generate_openapi.py": b"",
                "PKG-INFO": metadata,
            }
            for name, data in files.items():
                member = tarfile.TarInfo(f"rebrickable/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return directory

    return build


def test_conditional_runtime_floor_is_verified(distribution_factory) -> None:
    directory = distribution_factory(
        [
            "filelock >=3.20.3",
            'anyio>=4.14.2 ; sys_platform != "emscripten"',
            'requests>=2.33.0 ; extra == "ldraw"',
        ]
    )
    assert (
        check_distributions.verify(directory)
        == (directory / "rebrickable.whl").resolve()
    )


@pytest.mark.parametrize(
    "requirement",
    [
        None,
        "anyio>=4.10; sys_platform != 'emscripten'",
        "anyio>=4.14.2; sys_platform == 'emscripten'",
    ],
)
def test_missing_weaker_or_changed_conditional_floor_fails(
    distribution_factory, requirement: str | None
) -> None:
    requirements = ["filelock>=3.20.3", "requests>=2.33.0; extra == 'ldraw'"]
    if requirement is not None:
        requirements.append(requirement)
    with pytest.raises(ValueError, match="runtime dependencies differ"):
        check_distributions.verify(distribution_factory(requirements))


def test_weaker_extra_floor_fails(distribution_factory) -> None:
    directory = distribution_factory(
        [
            "filelock>=3.20.3",
            "anyio>=4.14.2; sys_platform != 'emscripten'",
            "requests>=2.32.4; extra == 'ldraw'",
        ]
    )
    with pytest.raises(ValueError, match="extra dependencies differ"):
        check_distributions.verify(directory)
