"""Task 1 — deterministic baseline.

Reads every case in input/, applies EC_POLICY_V2 with no LLM in the loop, and
writes output/EC_0xx.json. The result doubles as the ground truth the agent
run (task 2) is diffed against.

Usage:
    python run_baseline.py
    python run_baseline.py --out output_baseline
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

from src.data_store import OlistStore
from src.pipeline import solve_case

ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.path.join(ROOT, "data"))
    parser.add_argument("--input", default=os.path.join(ROOT, "input"))
    parser.add_argument("--out", default=os.path.join(ROOT, "output"))
    args = parser.parse_args()

    store = OlistStore(args.data)
    os.makedirs(args.out, exist_ok=True)

    case_files = sorted(glob.glob(os.path.join(args.input, "EC_*.json")))
    if not case_files:
        print(f"no case files under {args.input}", file=sys.stderr)
        return 1

    issue_counts: Counter = Counter()
    status_counts: Counter = Counter()
    total_refund = 0.0
    failures = []

    for path in case_files:
        with open(path, "r", encoding="utf-8") as handle:
            case = json.load(handle)

        doc, errors = solve_case(store, case)
        if errors:
            failures.append((case["case_id"], errors))

        target = os.path.join(args.out, f"{case['case_id']}.json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        issue_counts[doc["case_assessment"]["primary_issue"]] += 1
        status_counts[doc["case_assessment"]["case_status"]] += 1
        total_refund += doc["financial_resolution"]["recommended_refund_brl"]

    print(f"wrote {len(case_files)} cases -> {args.out}\n")
    print("primary issues:")
    for issue, count in issue_counts.most_common():
        print(f"  {issue:<26} {count:>3}")
    print("\ncase status:")
    for status, count in status_counts.most_common():
        print(f"  {status:<26} {count:>3}")
    print(f"\ntotal recommended refund: {round(total_refund, 2)} BRL")

    if failures:
        print(f"\nVERIFIER FAILURES ({len(failures)} cases):", file=sys.stderr)
        for case_id, errors in failures:
            for error in errors:
                print(f"  {case_id}: {error}", file=sys.stderr)
        return 1

    print("\nverifier: all cases clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
