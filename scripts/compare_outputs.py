"""Diff the agent run against the deterministic baseline.

Any field-level difference is a defect in one of the two, so this is the gate
that decides which directory gets submitted.

Usage:
    python scripts/compare_outputs.py output output_agents
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter


def flatten(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from flatten(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from flatten(value, f"{prefix}[{index}]")
    else:
        yield prefix, node


def main() -> int:
    left_dir = sys.argv[1] if len(sys.argv) > 1 else "output"
    right_dir = sys.argv[2] if len(sys.argv) > 2 else "output_agents"

    names = sorted(f for f in os.listdir(left_dir) if f.endswith(".json"))
    field_diffs: Counter = Counter()
    differing_cases = []
    missing = []

    for name in names:
        right_path = os.path.join(right_dir, name)
        if not os.path.exists(right_path):
            missing.append(name)
            continue
        with open(os.path.join(left_dir, name), encoding="utf-8") as handle:
            left = dict(flatten(json.load(handle)))
        with open(right_path, encoding="utf-8") as handle:
            right = dict(flatten(json.load(handle)))

        diffs = []
        for key in sorted(set(left) | set(right)):
            if left.get(key, "<absent>") != right.get(key, "<absent>"):
                diffs.append((key, left.get(key, "<absent>"), right.get(key, "<absent>")))
                field_diffs[key] += 1
        if diffs:
            differing_cases.append((name, diffs))

    print(f"compared {len(names) - len(missing)} cases: {left_dir} vs {right_dir}")
    print(f"identical : {len(names) - len(missing) - len(differing_cases)}")
    print(f"differing : {len(differing_cases)}")
    if missing:
        print(f"missing in {right_dir}: {missing}")

    for name, diffs in differing_cases:
        print(f"\n{name}")
        for key, left_value, right_value in diffs[:12]:
            print(f"  {key}\n    {left_dir}: {left_value!r}\n    {right_dir}: {right_value!r}")
        if len(diffs) > 12:
            print(f"  ... and {len(diffs) - 12} more fields")

    if field_diffs:
        print("\nfields differing most often:")
        for key, count in field_diffs.most_common(10):
            print(f"  {key:<50} {count}")

    return 1 if differing_cases or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
