"""Reject a dispatch whose checkout does not match its requested commit."""

from __future__ import annotations

import os
import re
import subprocess


def check_revision(expected: str) -> str:
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if expected and (
        re.fullmatch(r"[0-9a-f]{40}", expected) is None or actual != expected
    ):
        raise ValueError(f"expected revision {expected!r}; checked out {actual}")
    print(f"Validated revision {actual}")
    return actual


if __name__ == "__main__":
    check_revision(os.environ.get("EXPECTED_SHA") or os.environ.get("GITHUB_SHA", ""))
