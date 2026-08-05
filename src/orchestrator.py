"""Coordinator: runs the agent graph for one case and guards the result.

Hand-off order is customer -> order/product -> payment -> delivery -> policy ->
verifier. The coordinator owns two guarantees the agents cannot provide on
their own:

* every number in the document comes from a tool result, not from model prose;
* a classification that contradicts EC_POLICY_V2 is retried once, then
  overridden deterministically and recorded as an override in the trace.

Overrides are logged rather than hidden — the agreement rate is a headline
number in the report, so silently papering over a wrong verdict would make the
trace dishonest.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import agents, analysis, llm_client, llm_config, policy, schema

DOMAIN_AGENTS = ("customer", "order_product", "payment", "delivery")

_DELIVERY_KEYS = (
    "delivered_at", "estimated_delivery_at", "carrier_handoff_at",
    "delivery_variance_hours", "seller_handoff_analysis", "late_handoff_seller_ids",
)
_PAYMENT_KEYS = (
    "currency", "item_total_brl", "freight_total_brl", "expected_total_brl",
    "payment_total_brl", "difference_brl", "reconciled", "payment_types",
)


class TraceWriter:
    """Thread-safe JSONL trace. Truncated at construction: the brief wants the
    latest run only, not an ever-growing append log."""

    def __init__(self, path: str, run_id: str):
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        open(path, "w", encoding="utf-8").close()

    def emit(self, case_id: str, event: str, **fields) -> None:
        record = {
            "run_id": self.run_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "case_id": case_id,
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def for_case(self, case_id: str):
        def emit(event: str, **fields):
            self.emit(case_id, event, **fields)
        return emit


class Coordinator:
    def __init__(self, store, registry, trace: TraceWriter):
        self.store = store
        self.registry = registry
        self.trace = trace
        self.clients = {
            agent: llm_client.OpenRouterClient(model)
            for agent, model in llm_config.AGENT_MODELS.items()
        }

    # ------------------------------------------------------------------- run

    def run_case(self, case: dict) -> Tuple[dict, dict]:
        case_id = case["case_id"]
        order_id = case["customer_request"]["claimed_order_id"]
        emit = self.trace.for_case(case_id)
        started = time.time()

        emit("case_start", order_id=order_id,
             policy_version=case.get("policy_version"),
             scope=case.get("investigation_scope"))

        facts: Dict[str, dict] = {}
        handoffs: Dict[str, Optional[dict]] = {}
        forced: List[str] = []
        for agent in DOMAIN_AGENTS:
            emit("dispatch", agent=agent, by="coordinator")
            result = agents.run_domain_agent(
                agent, self.clients[agent], self.registry, order_id, emit
            )
            facts[agent] = result["facts"]
            handoffs[agent] = result["handoff"]
            if result["tool_forced"]:
                forced.append(agent)

        digest = agents.build_policy_digest(facts)
        emit("dispatch", agent="policy", by="coordinator", digest=digest)

        truth = self._deterministic_verdict(order_id, facts)
        accepted, agreement = self._decide(case_id, digest, truth, emit)

        doc = self._assemble(case_id, order_id, facts, accepted)
        schema_errors = schema.verify_case_output(doc, case_id, self.store)

        review = agents.run_verifier_agent(
            self.clients["verifier"], self._summarize(doc), schema_errors, emit
        )

        fallback = False
        if schema_errors:
            emit("fallback", reason="schema_errors", errors=schema_errors)
            doc = self._assemble(case_id, order_id, facts, truth)
            schema_errors = schema.verify_case_output(doc, case_id, self.store)
            fallback = True

        emit("case_end", primary_issue=doc["case_assessment"]["primary_issue"],
             refund_brl=doc["financial_resolution"]["recommended_refund_brl"],
             policy_agreement=agreement, verifier_approved=(review or {}).get("approved"),
             schema_errors=schema_errors, fallback=fallback,
             elapsed_ms=int((time.time() - started) * 1000))

        record = {
            "case_id": case_id,
            "agreement": agreement,
            "forced_tool_agents": forced,
            "verifier_approved": (review or {}).get("approved"),
            "schema_errors": schema_errors,
            "fallback": fallback,
            "handoffs": handoffs,
        }
        return doc, record

    # -------------------------------------------------------------- decision

    def _decide(self, case_id: str, digest: dict, truth: dict, emit) -> Tuple[dict, dict]:
        """Ask the Policy agent, check it against the table, retry once, then override."""
        expected_party = agents.expected_party_type(truth["primary_issue"])
        attempts = []

        for attempt in range(2):
            verdict = agents.run_policy_agent(self.clients["policy"], digest, emit)
            issues = self._agreement_issues(verdict, truth, expected_party)
            attempts.append({"attempt": attempt + 1, "verdict": verdict, "issues": issues})
            if not issues:
                emit("policy_accepted", attempt=attempt + 1,
                     primary_issue=verdict["primary_issue"])
                accepted = dict(truth)
                accepted["primary_issue"] = verdict["primary_issue"]
                # The agent selects *which* issues hold; the canonical ordering
                # is a schema invariant, so the coordinator sorts rather than
                # rejecting an otherwise correct answer over field order.
                accepted["secondary_issues"] = sorted(
                    set(verdict["secondary_issues"]), key=schema.SECONDARY_ISSUES.index
                )
                return accepted, {"agreed": True, "attempts": attempt + 1, "issues": []}

            emit("policy_rejected", attempt=attempt + 1, issues=issues,
                 proposed=(verdict or {}).get("primary_issue"))
            if attempt == 0:
                digest = dict(digest)
                digest["_verifier_feedback"] = (
                    "Your previous answer was rejected: " + "; ".join(issues) +
                    ". Re-read the table and answer strictly from the fields given."
                )

        emit("policy_override", final=truth["primary_issue"],
             reason="agent disagreed with EC_POLICY_V2 twice")
        return truth, {
            "agreed": False,
            "attempts": len(attempts),
            "issues": attempts[-1]["issues"],
        }

    @staticmethod
    def _agreement_issues(verdict: Optional[dict], truth: dict, expected_party: str) -> List[str]:
        if not isinstance(verdict, dict):
            return ["policy agent did not return parsable JSON"]
        issues = []
        if verdict.get("primary_issue") != truth["primary_issue"]:
            issues.append(
                f"primary_issue {verdict.get('primary_issue')!r} "
                f"does not follow the table (expected {truth['primary_issue']!r})"
            )
        secondary = verdict.get("secondary_issues")
        if not isinstance(secondary, list):
            issues.append(f"secondary_issues must be a list, got {secondary!r}")
        elif set(secondary) != set(truth["secondary_issues"]):
            missing = sorted(set(truth["secondary_issues"]) - set(secondary))
            extra = sorted(set(secondary) - set(truth["secondary_issues"]))
            detail = []
            if missing:
                detail.append(f"missing {missing}")
            if extra:
                detail.append(f"included {extra} whose input field is false")
            issues.append("secondary_issues wrong: " + ", ".join(detail))
        if verdict.get("responsible_party_type") != expected_party:
            issues.append(
                f"responsible_party_type {verdict.get('responsible_party_type')!r} "
                f"should be {expected_party!r}"
            )
        return issues

    # -------------------------------------------------------------- assembly

    def _deterministic_verdict(self, order_id: str, facts: Dict[str, dict]) -> dict:
        order = self.store.get_order(order_id)
        delivery = self._project(facts["delivery"], _DELIVERY_KEYS)
        payment = self._project(facts["payment"], _PAYMENT_KEYS)
        counts = {
            "item_count": facts["order_product"]["item_count"],
            "seller_count": facts["order_product"]["seller_count"],
            "category_count": facts["order_product"]["category_count"],
            "payment_count": facts["payment"]["payment_count"],
        }
        return policy.apply_policy(
            order, delivery, payment, counts,
            facts["customer"]["related_order_ids"],
            facts["delivery"]["late_delivery"],
        )

    def _assemble(self, case_id: str, order_id: str, facts: Dict[str, dict],
                  verdict: dict) -> dict:
        delivery = self._project(facts["delivery"], _DELIVERY_KEYS)
        payment = self._project(facts["payment"], _PAYMENT_KEYS)
        customer = {
            "customer_unique_id": facts["customer"]["customer_unique_id"],
            "related_order_ids": facts["customer"]["related_order_ids"],
        }
        products = {
            "product_ids": facts["order_product"]["product_ids"],
            "category_names": facts["order_product"]["category_names"],
        }
        counts = {
            "item_ids": facts["order_product"]["item_ids"],
            "seller_ids": facts["order_product"]["seller_ids"],
            "payment_ids": facts["payment"]["payment_ids"],
        }
        evidence_ids = policy.build_evidence_ids(
            order_id,
            counts["item_ids"][: schema.LIMITS["item_ids"]],
            counts["payment_ids"][: schema.LIMITS["payment_ids"]],
            verdict["responsible_parties"],
            verdict["root_cause_code"],
        )
        return schema.build_case_output(
            case_id, order_id, delivery, payment, customer, products,
            counts, verdict, evidence_ids,
        )

    @staticmethod
    def _project(source: dict, keys) -> dict:
        """Drop the extra fields the tools add for the agents' benefit; the
        submission schema must not carry them."""
        return {key: source[key] for key in keys}

    @staticmethod
    def _summarize(doc: dict) -> dict:
        return {
            "primary_issue": doc["case_assessment"]["primary_issue"],
            "secondary_issues": doc["case_assessment"]["secondary_issues"],
            "case_status": doc["case_assessment"]["case_status"],
            "recommended_refund_brl": doc["financial_resolution"]["recommended_refund_brl"],
            "payment_total_brl": doc["payment_reconciliation"]["payment_total_brl"],
            "freight_total_brl": doc["payment_reconciliation"]["freight_total_brl"],
            "evidence_count": len(doc["evidence_ids"]),
            "resolution_actions": doc["resolution_actions"],
        }

    def token_usage(self) -> dict:
        return {
            "calls": sum(c.call_count for c in self.clients.values()),
            "prompt_tokens": sum(c.total_prompt_tokens for c in self.clients.values()),
            "completion_tokens": sum(c.total_completion_tokens for c in self.clients.values()),
        }
