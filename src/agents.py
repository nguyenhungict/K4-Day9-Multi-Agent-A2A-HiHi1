"""Agent definitions: prompts, hand-off contracts and per-agent run loops.

Four domain agents each own one slice of the case, call the single tool they
are allowed to call, and hand a small JSON digest to the Policy agent. The
Policy agent classifies. The Verifier agent signs off before anything is
written.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

from . import llm_client, policy

# Shared framing. Kept short on purpose: 8B models lose instruction adherence
# fast as the system prompt grows.
_BASE_RULES = (
    "You are one specialist agent in an e-commerce dispute investigation team.\n"
    "Rules:\n"
    "1. Facts come only from your tool. Never invent IDs, dates or amounts.\n"
    "2. Never do arithmetic yourself — the tool already computed it.\n"
    "3. Reply with a single JSON object and nothing else.\n"
)

AGENT_PROMPTS: Dict[str, str] = {
    "customer": _BASE_RULES + (
        "You are the CUSTOMER agent. Call lookup_customer_history, then report:\n"
        '{"customer_unique_id": str, "related_order_count": int, '
        '"repeat_customer": bool, "note": str}\n'
        "repeat_customer is true when the shopper has any other order."
    ),
    "order_product": _BASE_RULES + (
        "You are the ORDER & PRODUCT agent. Call lookup_order_items, then report:\n"
        '{"order_status": str, "item_count": int, "seller_count": int, '
        '"category_count": int, "multi_seller": bool, "note": str}\n'
        "An order with zero item rows is normal for unavailable orders."
    ),
    "payment": _BASE_RULES + (
        "You are the PAYMENT agent. Call reconcile_order_payments, then report:\n"
        '{"payment_count": int, "payment_total_brl": number, '
        '"reconciled": bool|null, "split_payment": bool, "note": str}\n'
        "split_payment is true when payment_count >= 2. reconciled is null when "
        "the order has no item rows to compare against."
    ),
    "delivery": _BASE_RULES + (
        "You are the DELIVERY agent. Call analyze_order_delivery, then report:\n"
        '{"late_delivery": bool, "delivery_variance_hours": number|null, '
        '"late_handoff_seller_ids": [str], "blame_hint": str, "note": str}\n'
        "blame_hint is 'seller' when at least one seller handed off after its "
        "shipping limit, 'logistics' when delivery was late but every seller was "
        "on time, otherwise 'none'."
    ),
}

POLICY_PROMPT = (
    "You are the POLICY agent applying EC_POLICY_V2 to verified facts from four "
    "specialist agents.\n\n"
    "STEP 1 - primary_issue. Walk this table top to bottom, STOP at the first "
    "row whose condition is true:\n"
    "  canceled_order_paid       if order_status == 'canceled' and has_payment\n"
    "  unavailable_order_paid    if order_status == 'unavailable' and has_payment\n"
    "  late_delivery_seller      if late_delivery and late_handoff_seller_ids is non-empty\n"
    "  late_delivery_logistics   if late_delivery and late_handoff_seller_ids is empty\n"
    "  valid_split_payment       if split_payment and reconciled == true\n"
    "  unsupported_late_claim    otherwise\n\n"
    "STEP 2 - responsible_party_type, decided by the row you picked:\n"
    "  canceled_order_paid, unavailable_order_paid -> 'platform'\n"
    "  late_delivery_seller -> 'seller'\n"
    "  late_delivery_logistics -> 'logistics_provider'\n"
    "  valid_split_payment, unsupported_late_claim -> 'none'\n\n"
    "STEP 3 - secondary_issues. Copy the names of the input fields that are "
    "exactly true, choosing only from: multi_item_order, multi_seller_order, "
    "split_payment, repeat_customer, multiple_categories. Do NOT include a name "
    "whose field is false. Do NOT invent names.\n\n"
    "Example input: {\"order_status\":\"delivered\", \"has_payment\":true, "
    "\"late_delivery\":true, \"late_handoff_seller_ids\":[], \"reconciled\":true, "
    "\"multi_item_order\":true, \"multi_seller_order\":false, \"split_payment\":false, "
    "\"repeat_customer\":true, \"multiple_categories\":false}\n"
    "Example output: {\"primary_issue\":\"late_delivery_logistics\", "
    "\"secondary_issues\":[\"multi_item_order\",\"repeat_customer\"], "
    "\"responsible_party_type\":\"logistics_provider\", "
    "\"reasoning\":\"Delivered late but every seller met its shipping limit.\"}\n\n"
    "Reply with one JSON object using exactly those four keys. "
    "Keep reasoning under 25 words."
)

VERIFIER_PROMPT = (
    "You are the VERIFIER agent, the last gate before the case file is written.\n"
    "You receive the assembled case document summary and the output of a "
    "deterministic schema check. Confirm that:\n"
    "- the primary issue matches the facts,\n"
    "- refund is the full payment total for canceled/unavailable, the freight "
    "total for late delivery, and 0 otherwise,\n"
    "- case_status is action_required exactly when refund > 0,\n"
    "- the schema check reported no errors.\n\n"
    "Reply with one JSON object:\n"
    '{"approved": bool, "issues": [str], "note": str}\n'
    "Set approved to false if the schema check listed any error."
)


class AgentResult(dict):
    """Plain dict; named for readability at call sites."""


def run_domain_agent(agent: str, client: llm_client.OpenRouterClient,
                     registry, order_id: str, trace: Callable) -> AgentResult:
    """One domain agent: call its tool, then emit a JSON hand-off.

    If the model fails to emit a tool call, the coordinator invokes the tool on
    its behalf and records `tool_forced`. Investigation must not stall because
    a small model skipped its turn.
    """
    tool_name = registry.specs_for(agent)[0]["function"]["name"]
    messages: List[Dict] = [
        {"role": "system", "content": AGENT_PROMPTS[agent]},
        {"role": "user", "content": f"Investigate order_id {order_id}. Call your tool first."},
    ]

    first = client.chat(messages, tools=registry.specs_for(agent), tool_choice="required")
    trace("llm_call", agent=agent, phase="tool_selection", tokens_in=first["prompt_tokens"],
          tokens_out=first["completion_tokens"], latency_ms=first["latency_ms"],
          tool_calls=[c.get("function", {}).get("name") for c in first["tool_calls"]])

    tool_forced = not first["tool_calls"]
    if tool_forced:
        facts = registry.call(agent, tool_name, {"order_id": order_id})
        trace("tool_call", agent=agent, tool=tool_name, forced=True)
        messages.append({"role": "assistant", "content": f"I will call {tool_name}."})
        messages.append({"role": "user", "content": f"{tool_name} returned: {json.dumps(facts)}"})
    else:
        call = first["tool_calls"][0]
        requested = call.get("function", {}).get("name", tool_name)
        try:
            arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        # The model occasionally garbles the argument; the case's order id is
        # authoritative, so repair rather than fail.
        if arguments.get("order_id") != order_id:
            arguments = {"order_id": order_id}
        facts = registry.call(agent, requested, arguments)
        trace("tool_call", agent=agent, tool=requested, forced=False)
        messages.append(first["message"])
        messages.append({
            "role": "tool",
            "tool_call_id": call.get("id", "call_0"),
            "name": requested,
            "content": json.dumps(facts),
        })

    second = client.chat(messages, json_object=True)
    handoff = llm_client.parse_json_content(second["content"])
    trace("llm_call", agent=agent, phase="handoff", tokens_in=second["prompt_tokens"],
          tokens_out=second["completion_tokens"], latency_ms=second["latency_ms"],
          parsed=handoff is not None)
    trace("handoff", agent=agent, to="policy", payload=handoff)

    return AgentResult(agent=agent, facts=facts, handoff=handoff, tool_forced=tool_forced)


def run_policy_agent(client: llm_client.OpenRouterClient, digest: dict,
                     trace: Callable) -> Optional[dict]:
    messages = [
        {"role": "system", "content": POLICY_PROMPT},
        {"role": "user", "content": json.dumps(digest)},
    ]
    response = client.chat(messages, json_object=True)
    verdict = llm_client.parse_json_content(response["content"])
    trace("llm_call", agent="policy", phase="classification",
          tokens_in=response["prompt_tokens"], tokens_out=response["completion_tokens"],
          latency_ms=response["latency_ms"], parsed=verdict is not None)
    trace("handoff", agent="policy", to="coordinator", payload=verdict)
    return verdict


def run_verifier_agent(client: llm_client.OpenRouterClient, summary: dict,
                       schema_errors: List[str], trace: Callable) -> Optional[dict]:
    messages = [
        {"role": "system", "content": VERIFIER_PROMPT},
        {"role": "user", "content": json.dumps(
            {"document_summary": summary, "schema_check_errors": schema_errors}
        )},
    ]
    response = client.chat(messages, json_object=True)
    review = llm_client.parse_json_content(response["content"])
    trace("llm_call", agent="verifier", phase="review",
          tokens_in=response["prompt_tokens"], tokens_out=response["completion_tokens"],
          latency_ms=response["latency_ms"], parsed=review is not None)
    trace("handoff", agent="verifier", to="coordinator", payload=review)
    return review


def build_policy_digest(facts: Dict[str, dict]) -> dict:
    """Compact, tool-derived view handed to the Policy agent.

    Only the fields the policy table keys on: an 8B model classifies far more
    reliably when the prompt carries no distractors.
    """
    order = facts["order_product"]
    payment = facts["payment"]
    delivery = facts["delivery"]
    customer = facts["customer"]
    return {
        "order_status": order["order_status"],
        "payment_total_brl": payment["payment_total_brl"],
        "freight_total_brl": payment["freight_total_brl"],
        "reconciled": payment["reconciled"],
        "late_delivery": delivery["late_delivery"],
        "late_handoff_seller_ids": delivery["late_handoff_seller_ids"],
        # Threshold comparisons are pre-evaluated by the tools. An 8B model
        # reliably *selects* from booleans but is unreliable at re-deriving
        # "seller_count >= 2" from a raw count, which is how it invented
        # multi_seller_order on single-seller orders during the smoke test.
        "has_payment": payment["payment_total_brl"] > 0,
        "multi_item_order": order["item_count"] >= 2,
        "multi_seller_order": order["seller_count"] >= 2,
        "split_payment": payment["payment_count"] >= 2,
        "repeat_customer": customer["repeat_customer"],
        "multiple_categories": order["category_count"] >= 2,
    }


def expected_party_type(primary_issue: str) -> str:
    if primary_issue in (policy.CANCELED_ORDER_PAID, policy.UNAVAILABLE_ORDER_PAID):
        return "platform"
    if primary_issue == policy.LATE_DELIVERY_SELLER:
        return "seller"
    if primary_issue == policy.LATE_DELIVERY_LOGISTICS:
        return "logistics_provider"
    return "none"
