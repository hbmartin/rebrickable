"""Install pinned Linux workflow linters, verifying release archive SHA-256 digests."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

# Digests from the official actionlint checksum manifest and GitHub release assets.
TOOLS = {
    "actionlint": (
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    "zizmor": (
        "https://github.com/zizmorcore/zizmor/releases/download/v1.30.1/zizmor-x86_64-unknown-linux-gnu.tar.gz",
        "e65324f4430c2717591937edcec90ccbefaf14c174f8ec9415e03ca875b46e1a",
    ),
    "shellcheck": (
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.xz",
        "8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198",
    ),
}


def install(directory: Path) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError(
            "installer targets Linux x86_64 CI; locally use the pinned versions documented in README"
        )
    directory.mkdir(parents=True, exist_ok=True)
    for tool, (url, digest) in TOOLS.items():
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed HTTPS release allowlist
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"checksum mismatch: {tool}")
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            (member,) = (
                member
                for member in archive.getmembers()
                if member.isfile() and Path(member.name).name == tool
            )
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"missing executable: {tool}")
            with handle:
                executable = directory / tool
                executable.write_bytes(handle.read())
                executable.chmod(0o755)
        print(f"Installed checksum-verified {tool}")
    if path := os.environ.get("GITHUB_PATH"):
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{directory.resolve()}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".tools"))
    install(parser.parse_args().directory)
