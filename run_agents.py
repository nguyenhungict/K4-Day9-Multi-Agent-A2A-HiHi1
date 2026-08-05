"""Task 2 — multi-agent run over the 50 cases.

Executes the coordinator/specialist/policy/verifier graph against OpenRouter,
writes output/, a fresh trace.jsonl and metadata.json, and reports how often
the Policy agent agreed with EC_POLICY_V2 without needing an override.

Usage:
    python run_agents.py                  # all 50 cases
    python run_agents.py --limit 3        # smoke test
    python run_agents.py --workers 6
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from src import llm_client, llm_config
from src.agent_tools import AGENT_TOOL_ACCESS, ToolRegistry
from src.data_store import OlistStore
from src.orchestrator import Coordinator, TraceWriter

ROOT = os.path.dirname(os.path.abspath(__file__))


def write_metadata(path: str, run_id: str, usage: dict, elapsed_s: float,
                   case_count: int, agreement: dict) -> dict:
    metadata = {
        "run_id": run_id,
        "lab": "K4 Day 09 - Multi-Agent E-commerce Dispute Resolution",
        "policy_version": "EC_POLICY_V2",
        "framework": "custom multi-agent orchestrator (no agent framework); "
                     "OpenRouter chat-completions tool calling over urllib",
        "provider": "OpenRouter",
        "models": [
            {
                "model": name,
                "parameters_b": spec["parameters_b"],
                "context_length": spec["context"],
                "tool_calling": spec["tools"],
                "within_10b_cap": spec["parameters_b"] <= 10,
            }
            for name, spec in llm_config.models_in_use().items()
        ],
        "agent_models": llm_config.AGENT_MODELS,
        "agent_tool_access": AGENT_TOOL_ACCESS,
        "generation": llm_config.GENERATION,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dependencies": "standard library only",
        },
        "run": {
            "cases": case_count,
            "elapsed_seconds": round(elapsed_s, 1),
            "llm_calls": usage["calls"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "policy_agreement_rate": agreement["rate"],
            "policy_overrides": agreement["overrides"],
            "schema_failures": agreement["schema_failures"],
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.path.join(ROOT, "data"))
    parser.add_argument("--input", default=os.path.join(ROOT, "input"))
    parser.add_argument("--out", default=os.path.join(ROOT, "output"))
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    llm_client.load_env(os.path.join(ROOT, ".env"))
    llm_config.assert_within_param_cap(10)
    llm_config.get_api_key()  # fail fast before loading 40 MB of CSV

    store = OlistStore(args.data)
    registry = ToolRegistry(store)

    case_files = sorted(glob.glob(os.path.join(args.input, "EC_*.json")))
    if args.limit:
        case_files = case_files[: args.limit]
    cases = [json.load(open(path, "r", encoding="utf-8")) for path in case_files]

    run_id = time.strftime("%Y%m%dT%H%M%S")
    os.makedirs(os.path.join(ROOT, "logging"), exist_ok=True)
    trace_path = os.path.join(ROOT, "logging", "trace.jsonl")
    trace = TraceWriter(trace_path, run_id)
    coordinator = Coordinator(store, registry, trace)

    os.makedirs(args.out, exist_ok=True)
    print(f"run {run_id} | {len(cases)} cases | model {llm_config.PRIMARY_MODEL} "
          f"| {args.workers} workers\n")

    started = time.time()
    results = {}
    failures = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(coordinator.run_case, case): case for case in cases}
        for done, future in enumerate(as_completed(futures), 1):
            case = futures[future]
            try:
                doc, record = future.result()
            except Exception as exc:  # keep going; report at the end
                failures.append((case["case_id"], f"{type(exc).__name__}: {exc}"))
                print(f"  [{done:>2}/{len(cases)}] {case['case_id']} FAILED: {exc}")
                continue

            target = os.path.join(args.out, f"{case['case_id']}.json")
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            results[case["case_id"]] = record

            flag = "ok " if record["agreement"]["agreed"] else "OVR"
            print(f"  [{done:>2}/{len(cases)}] {case['case_id']} {flag} "
                  f"{doc['case_assessment']['primary_issue']}")

    elapsed = time.time() - started

    agreed = sum(1 for r in results.values() if r["agreement"]["agreed"])
    overrides = len(results) - agreed
    schema_failures = sum(1 for r in results.values() if r["schema_errors"])
    forced = sum(len(r["forced_tool_agents"]) for r in results.values())
    approved = sum(1 for r in results.values() if r["verifier_approved"] is True)
    issue_counts = Counter()
    for case_id in results:
        with open(os.path.join(args.out, f"{case_id}.json"), encoding="utf-8") as handle:
            issue_counts[json.load(handle)["case_assessment"]["primary_issue"]] += 1

    agreement = {
        "rate": round(agreed / len(results), 4) if results else 0.0,
        "overrides": overrides,
        "schema_failures": schema_failures,
    }
    usage = coordinator.token_usage()

    metadata_path = os.path.join(ROOT, "logging", "metadata.json")
    write_metadata(metadata_path, run_id, usage, elapsed, len(results), agreement)
    # The brief asks for both artifacts at the repo root as well.
    shutil.copyfile(metadata_path, os.path.join(ROOT, "metadata.json"))
    shutil.copyfile(trace_path, os.path.join(ROOT, "trace.jsonl"))

    print(f"\ncases written      : {len(results)}/{len(cases)}")
    print(f"elapsed            : {elapsed:.1f}s")
    print(f"llm calls          : {usage['calls']} "
          f"({usage['prompt_tokens']} in / {usage['completion_tokens']} out tokens)")
    print(f"policy agreement   : {agreed}/{len(results)} ({agreement['rate']:.0%})")
    print(f"policy overrides   : {overrides}")
    print(f"verifier approved  : {approved}/{len(results)}")
    print(f"forced tool calls  : {forced}")
    print(f"schema failures    : {schema_failures}")
    print("\nprimary issues:")
    for issue, count in issue_counts.most_common():
        print(f"  {issue:<26} {count:>3}")

    if failures:
        print(f"\nFAILED CASES ({len(failures)}):", file=sys.stderr)
        for case_id, error in failures:
            print(f"  {case_id}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
