"""Coverage gates from docs/TESTING.md §2.4. Usage: python tools/coverage_gate.py coverage.json"""

from __future__ import annotations

import json
import sys

STRICT = ("c2a/core/", "c2a/board/", "c2a/prompts/")
STRICT_LINE, STRICT_BRANCH, TOTAL_LINE = 90.0, 85.0, 80.0


def pct(hit: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * hit / total


def main(path: str) -> int:
    with open(path) as f:
        data = json.load(f)
    failed = False
    for prefix in STRICT:
        lines = hit = br = br_hit = 0
        for name, fdata in data["files"].items():
            if not name.startswith(prefix):
                continue
            s = fdata["summary"]
            lines += s["num_statements"]
            hit += s["covered_lines"]
            br += s.get("num_branches", 0)
            br_hit += s.get("covered_branches", 0)
        lp, bp = pct(hit, lines), pct(br_hit, br)
        ok = lp >= STRICT_LINE and bp >= STRICT_BRANCH
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {prefix:<14} line {lp:5.1f}% (≥{STRICT_LINE})  "
              f"branch {bp:5.1f}% (≥{STRICT_BRANCH})")
    t = data["totals"]
    tp = pct(t["covered_lines"], t["num_statements"])
    ok = tp >= TOTAL_LINE
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'total':<14} line {tp:5.1f}% (≥{TOTAL_LINE})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
