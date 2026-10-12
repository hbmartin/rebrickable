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
    (root / "LICENSE.txt").write_bytes(b"fixture license\nsecond line\n")
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

    def build(
        requirements: list[str],
        *,
        wheel_licenses: tuple[bytes, ...] = (b"fixture license\nsecond line\n",),
        sdist_licenses: tuple[bytes, ...] = (b"fixture license\nsecond line\n",),
    ) -> Path:
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
            for index, content in enumerate(wheel_licenses):
                archive.writestr(
                    f"rebrickable{index}.dist-info/licenses/LICENSE.txt", content
                )
        with tarfile.open(directory / "rebrickable.tar.gz", "w:gz") as archive:
            files = {
                "docs/api.md": b"",
                "tests/test_catalog_session.py": b"",
                "scripts/generate_openapi.py": b"",
                "PKG-INFO": metadata,
            }
            files.update(
                {
                    f"licenses{index}/LICENSE.txt": content
                    for index, content in enumerate(sdist_licenses)
                }
            )
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


REQUIREMENTS = [
    "filelock>=3.20.3",
    "anyio>=4.14.2; sys_platform != 'emscripten'",
    "requests>=2.33.0; extra == 'ldraw'",
]


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_checkout_and_distribution_license_line_endings(
    distribution_factory, line_ending: bytes
) -> None:
    check_distributions.ROOT.joinpath("LICENSE.txt").write_bytes(
        b"fixture license\nsecond line\n".replace(b"\n", line_ending)
    )
    directory = distribution_factory(
        REQUIREMENTS,
        sdist_licenses=(b"fixture license\r\nsecond line\r\n",),
    )
    check_distributions.verify(directory)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "licenses",
    [(), (b"changed content",), (b"fixture license\nsecond line",), (b"one", b"two")],
)
def test_missing_changed_or_duplicate_license_fails(
    distribution_factory, artifact: str, licenses: tuple[bytes, ...]
) -> None:
    with pytest.raises(ValueError, match=f"{artifact} license"):
        check_distributions.verify(
            distribution_factory(REQUIREMENTS, **{f"{artifact}_licenses": licenses})
        )
