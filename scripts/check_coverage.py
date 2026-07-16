"""Enforce the current coverage floor and report the product target honestly."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LINE_FLOOR = 90.0
BRANCH_FLOOR = 85.0
LINE_TARGET = 90.0
BRANCH_TARGET = 85.0


def main(path: Path) -> int:
    totals = json.loads(path.read_text(encoding="utf-8"))["totals"]
    line = 100 * totals["covered_lines"] / max(totals["num_statements"], 1)
    branch = 100 * totals["covered_branches"] / max(totals["num_branches"], 1)
    print(f"coverage: {line:.2f}% lines, {branch:.2f}% branches")
    print(f"product target: {LINE_TARGET:.0f}% lines, {BRANCH_TARGET:.0f}% branches")
    if line < LINE_FLOOR or branch < BRANCH_FLOOR:
        print(
            f"coverage floor failed: required {LINE_FLOOR:.0f}% lines and {BRANCH_FLOOR:.0f}% branches",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")))
