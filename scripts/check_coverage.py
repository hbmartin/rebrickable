"""Enforce branch coverage independently of Coverage.py's combined score."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


def check_coverage(path: Path, minimum: Decimal = Decimal(95)) -> str:
    if not minimum.is_finite() or not 0 <= minimum <= 100:
        raise ValueError("branch threshold must be finite and between 0 and 100")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("meta"), dict):
        raise ValueError("coverage report lacks metadata")  # noqa: TRY004 - malformed report, not a caller type error
    if document["meta"].get("branch_coverage") is not True:
        raise ValueError("coverage report does not measure branches")
    totals = document.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("coverage report lacks totals")  # noqa: TRY004 - malformed report, not a caller type error
    total = totals.get("num_branches")
    covered = totals.get("covered_branches")
    if type(total) is not int or type(covered) is not int:
        raise ValueError("branch totals must be integers")
    if total <= 0 or not 0 <= covered <= total:
        raise ValueError("invalid branch totals")
    percentage = Decimal(100) * covered / total
    numerator, denominator = minimum.as_integer_ratio()
    if 100 * covered * denominator < numerator * total:
        raise ValueError(f"branch coverage {percentage:.2f}% is below {minimum}%")
    return f"Branch coverage: {covered}/{total} ({percentage:.2f}%), minimum {minimum}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--minimum", type=Decimal, default=Decimal(95))
    args = parser.parse_args()
    try:
        print(check_coverage(args.report, args.minimum))
    except (OSError, ValueError, InvalidOperation) as exc:
        parser.exit(1, f"coverage check failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
