"""Copied into a temporary directory and run by the installed environment."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pickle
import socket
import subprocess
import sys
from importlib import metadata, resources
from pathlib import Path
from unittest.mock import patch

from catalog_fixture import build_catalog_config  # ty: ignore[unresolved-import]

import rebrickable
from rebrickable import (
    ApiThrottledError,
    Bom,
    ColorRef,
    Config,
    LDrawBomItem,
    PartRef,
    RebrickableSession,
)
from rebrickable.api.models import ApiPart
from rebrickable.errors import OptionalDependencyError
from rebrickable.progress import ProgressEvent, ProgressStage


def check_installation() -> Path:
    package = Path(rebrickable.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert not package.is_relative_to(Path.cwd()), package
    assert rebrickable.__version__ == metadata.version("rebrickable")
    assert resources.files("rebrickable").joinpath("py.typed").is_file()
    assert metadata.metadata("rebrickable")["License-Expression"] == "GPL-3.0-or-later"
    assert (
        ApiPart.model_validate(
            {"part_num": "3001", "name": "Brick", "custom": 42}
        ).extra["custom"]
        == 42
    )
    assert Config(Path("catalog.sqlite")).database_path == Path("catalog.sqlite")
    assert PartRef("ldraw", "3001.dat") == PartRef(system="ldraw", value="3001")
    assert ColorRef("ldraw", "4") == ColorRef(system="ldraw", value=4)
    assert LDrawBomItem("3001.dat", 4, 2).quantity == 2
    assert ProgressEvent(ProgressStage.DOWNLOAD).stage is ProgressStage.DOWNLOAD
    error = ApiThrottledError(429, "/parts/", "parts", "slow down")
    assert pickle.loads(pickle.dumps(error)) == error  # noqa: S301 - owned smoke value
    return package


async def check_catalog() -> None:
    config: Config = build_catalog_config(Path.cwd() / "catalog")
    async with await RebrickableSession.connect(config) as session:
        assert (await session.opened_catalog_state()).schema_version == 1
        assert (await session.parts.require("3001")).name == "Brick 2 x 4"
        assert len(await session.parts.list(limit=1)) == 1
        assert (await session.sets.bill_of_materials("100-1")).rows
        bom = Bom.from_rebrickable_csv("part_num,color_id,quantity\n3001,4,2\n")
        assert (await session.validate_bom(bom)).exact_count == 1
        assert (
            await session.ldraw.resolve_ldraw_part("3001.dat")
        ).target_identifier == "3001"
        if sys.argv[1] == "ldraw":
            assert importlib.util.find_spec("ldraw") is not None
            model = Path.cwd() / "simple.ldr"
            model.write_text("0 Fixture\n1 4 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n")
            assert (await session.ldraw.translate_model_path(model)).rows
        else:
            assert importlib.util.find_spec("ldraw") is None
            try:
                await session.ldraw.translate_model(object())
            except OptionalDependencyError:
                pass
            else:
                raise AssertionError("optional dependency guard did not fire")


def check_cli() -> None:
    cli = [
        str(
            Path(sys.prefix)
            / ("Scripts/rebrickable.exe" if os.name == "nt" else "bin/rebrickable")
        )
    ]
    assert subprocess.run(
        [*cli, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    spec = Path.cwd() / "openapi.json"
    subprocess.run([*cli, "api-spec", "--output", str(spec)], check=True)
    assert json.loads(spec.read_text())["swagger"] == "2.0"
    for unbuffered in (False, True):
        for arguments in (
            ["url", "part", "3001"],
            ["--help"],
            ["--version"],
            ["api-spec"],
        ):
            _check_closed_pipe(
                [*cli, *arguments], stream="stdout", status=0, unbuffered=unbuffered
            )
        _check_closed_pipe(
            [*cli, "unknown-command"], stream="stderr", status=2, unbuffered=unbuffered
        )
        for status in (3, 4, 5):
            code = f"""
from rebrickable import cli
async def run(args):
    cli._write_stdout('result', status={status})
    return {status}
cli._run = run
raise SystemExit(cli._entrypoint(['url', 'part', '3001']))
"""
            _check_closed_pipe(
                [sys.executable, "-c", code],
                stream="stdout",
                status=status,
                unbuffered=unbuffered,
            )


def _check_closed_pipe(
    command: list[str], *, stream: str, status: int, unbuffered: bool
) -> None:
    reader, writer = os.pipe()
    os.close(reader)
    try:
        result = subprocess.run(
            command,
            stdout=writer if stream == "stdout" else subprocess.PIPE,
            stderr=writer if stream == "stderr" else subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1" if unbuffered else "0"},
            timeout=15,
            check=False,
        )
    finally:
        os.close(writer)
    assert result.returncode == status, (command, result.stderr)
    assert result.stdout in (None, b"")
    assert result.stderr in (None, b"")


def _check_offline() -> Path:
    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("installed offline check attempted networking")

    with asyncio.Runner() as runner:
        # Windows creates a TCP socketpair while initializing the event loop.
        runner.get_loop()
        with (
            patch.object(socket, "create_connection", deny_network),
            patch.object(socket, "getaddrinfo", deny_network),
            patch.object(socket.socket, "connect", deny_network),
            patch.object(socket.socket, "connect_ex", deny_network),
        ):
            package = check_installation()
            runner.run(check_catalog())
            check_cli()
    return package


if __name__ == "__main__":
    package = _check_offline()
    print(f"Installed {sys.argv[1]} checks passed: {package}")
