"""Enforce Linux process peak RSS while preserving command output and diagnostics."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--max-rss-mib", type=float, default=512)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    raw = args.report.with_suffix(".txt")
    with args.stdout.open("w", encoding="utf-8") as output:
        completed = subprocess.run(
            ["/usr/bin/time", "-v", "-o", str(raw), *command],
            stdout=output,
            check=False,
        )
    match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", raw.read_text())
    if match is None:
        raise ValueError("GNU time did not report peak RSS")
    rss = int(match.group(1)) / 1024
    args.report.write_text(
        json.dumps(
            {
                "peak_rss_mib": rss,
                "maximum_rss_mib": args.max_rss_mib,
                "exit_code": completed.returncode,
            }
        )
        + "\n"
    )
    if rss > args.max_rss_mib:
        raise RuntimeError(f"peak RSS {rss:.2f} MiB exceeds {args.max_rss_mib} MiB")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
