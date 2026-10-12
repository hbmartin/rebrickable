from __future__ import annotations

import asyncio
import importlib
import socket
import sys
from pathlib import Path

import pytest

from tests import catalog_fixture


@pytest.mark.parametrize("fail_catalog", [False, True])
def test_loop_bootstraps_before_guards_and_teardown_restores_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fail_catalog: bool
) -> None:
    monkeypatch.setitem(sys.modules, "catalog_fixture", catalog_fixture)
    installed = importlib.import_module("tests.installed_check")
    bootstrap_calls: list[object] = []

    def bootstrap_connect(*args: object) -> None:
        bootstrap_calls.append(args)

    monkeypatch.setattr(socket.socket, "connect", bootstrap_connect)
    original_get_loop = asyncio.Runner.get_loop
    original_close = asyncio.Runner.close

    def get_loop(runner: asyncio.Runner) -> asyncio.AbstractEventLoop:
        # Model Windows' socketpair connect during event-loop initialization.
        socket.socket.connect(object(), ("127.0.0.1", 0))
        return original_get_loop(runner)

    def close(runner: asyncio.Runner) -> None:
        assert socket.socket.connect is bootstrap_connect
        original_close(runner)

    def assert_offline() -> Path:
        for connect in (socket.socket.connect, socket.socket.connect_ex):
            with pytest.raises(AssertionError, match="attempted networking"):
                connect(object(), ("example.test", 443))
        with pytest.raises(AssertionError, match="attempted networking"):
            socket.create_connection(("127.0.0.1", 443))
        with pytest.raises(AssertionError, match="attempted networking"):
            socket.getaddrinfo("example.test", 443)
        return tmp_path

    async def check_catalog() -> None:
        assert_offline()
        if fail_catalog:
            raise ValueError("catalog failed")

    monkeypatch.setattr(asyncio.Runner, "get_loop", get_loop)
    monkeypatch.setattr(asyncio.Runner, "close", close)
    monkeypatch.setattr(installed, "check_installation", assert_offline)
    monkeypatch.setattr(installed, "check_catalog", check_catalog)
    monkeypatch.setattr(installed, "check_cli", assert_offline)
    if fail_catalog:
        with pytest.raises(ValueError, match="catalog failed"):
            installed._check_offline()
    else:
        assert installed._check_offline() == tmp_path
    assert len(bootstrap_calls) == 1
