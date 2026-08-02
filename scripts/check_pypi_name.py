"""Fail trusted publication when the PyPI name belongs to another project."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def main() -> int:
    request = urllib.request.Request(
        "https://pypi.org/pypi/rebrickable/json",
        headers={
            "Accept": "application/json",
            "User-Agent": "rebrickable-release-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("PyPI name rebrickable is unclaimed")
            return 0
        raise
    urls = payload.get("info", {}).get("project_urls") or {}
    owned = any(
        "github.com/hbmartin/rebrickable" in str(value).casefold()
        for value in urls.values()
    )
    if not owned:
        raise SystemExit("PyPI name 'rebrickable' is owned by another project")
    print("PyPI project ownership metadata matches this repository")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
