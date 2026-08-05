"""EC_POLICY_V2 — issue taxonomy, responsibility, refund and actions.

The six primary branches are mutually exclusive and evaluated strictly in the
priority order given by the brief: a paid-but-dead order outranks a late
delivery, and a late delivery outranks any payment-shaped explanation.
"""

from __future__ import annotations

from typing import List, Optional

POLICY_VERSION = "EC_POLICY_V2"

CANCELED_ORDER_PAID = "canceled_order_paid"
UNAVAILABLE_ORDER_PAID = "unavailable_order_paid"
LATE_DELIVERY_SELLER = "late_delivery_seller"
LATE_DELIVERY_LOGISTICS = "late_delivery_logistics"
VALID_SPLIT_PAYMENT = "valid_split_payment"
UNSUPPORTED_LATE_CLAIM = "unsupported_late_claim"

# Deterministic per-branch confidence. Branches decided by a single hard field
# (order_status + money moved) are the most certain; the ones that rest on a
# tolerance comparison are scored slightly lower.
CONFIDENCE = {
    CANCELED_ORDER_PAID: 0.96,
    UNAVAILABLE_ORDER_PAID: 0.96,
    LATE_DELIVERY_SELLER: 0.93,
    LATE_DELIVERY_LOGISTICS: 0.91,
    VALID_SPLIT_PAYMENT: 0.89,
    UNSUPPORTED_LATE_CLAIM: 0.87,
}

ROOT_CAUSE = {
    CANCELED_ORDER_PAID: "ORDER_CANCELED_AFTER_PAYMENT",
    UNAVAILABLE_ORDER_PAID: "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    LATE_DELIVERY_SELLER: "SELLER_HANDOFF_AFTER_LIMIT",
    LATE_DELIVERY_LOGISTICS: "CARRIER_DELIVERED_AFTER_ESTIMATE",
    VALID_SPLIT_PAYMENT: "MULTIPLE_PAYMENTS_RECONCILED",
    UNSUPPORTED_LATE_CLAIM: "DELIVERY_WITHIN_ESTIMATE",
}

PRIMARY_ACTION = {
    CANCELED_ORDER_PAID: "issue_full_refund",
    UNAVAILABLE_ORDER_PAID: "issue_full_refund",
    LATE_DELIVERY_SELLER: "refund_freight",
    LATE_DELIVERY_LOGISTICS: "refund_freight",
    VALID_SPLIT_PAYMENT: "explain_valid_split_payment",
    UNSUPPORTED_LATE_CLAIM: "reject_late_refund",
}

PLATFORM_PARTY_ID = "OLIST_PLATFORM"
LOGISTICS_PARTY_ID = "LOGISTICS_PROVIDER"


def classify_primary_issue(order: dict, delivery: dict, payment: dict,
                           payment_count: int, late: bool) -> str:
    status = order["order_status"]
    paid = payment["payment_total_brl"] > 0

    if status == "canceled" and paid:
        return CANCELED_ORDER_PAID
    if status == "unavailable" and paid:
        return UNAVAILABLE_ORDER_PAID
    if late:
        return (
            LATE_DELIVERY_SELLER
            if delivery["late_handoff_seller_ids"]
            else LATE_DELIVERY_LOGISTICS
        )
    if payment_count >= 2 and payment["reconciled"] is True:
        return VALID_SPLIT_PAYMENT
    return UNSUPPORTED_LATE_CLAIM


def derive_secondary_issues(counts: dict, related_order_ids: List[str]) -> List[str]:
    """Fixed evaluation order; the brief scores the sequence, not just the set."""
    issues = []
    if counts["item_count"] >= 2:
        issues.append("multi_item_order")
    if counts["seller_count"] >= 2:
        issues.append("multi_seller_order")
    if counts["payment_count"] >= 2:
        issues.append("split_payment")
    if related_order_ids:
        issues.append("repeat_customer")
    if counts["category_count"] >= 2:
        issues.append("multiple_categories")
    return issues


def derive_responsible_parties(primary_issue: str, delivery: dict) -> List[dict]:
    if primary_issue in (CANCELED_ORDER_PAID, UNAVAILABLE_ORDER_PAID):
        return [{"party_type": "platform", "party_id": PLATFORM_PARTY_ID}]
    if primary_issue == LATE_DELIVERY_SELLER:
        return [
            {"party_type": "seller", "party_id": seller_id}
            for seller_id in delivery["late_handoff_seller_ids"]
        ]
    if primary_issue == LATE_DELIVERY_LOGISTICS:
        return [{"party_type": "logistics_provider", "party_id": LOGISTICS_PARTY_ID}]
    return []


def derive_refund(primary_issue: str, payment: dict) -> float:
    if primary_issue in (CANCELED_ORDER_PAID, UNAVAILABLE_ORDER_PAID):
        return payment["payment_total_brl"]
    if primary_issue in (LATE_DELIVERY_SELLER, LATE_DELIVERY_LOGISTICS):
        return payment["freight_total_brl"]
    return 0.0


def derive_actions(primary_issue: str, secondary_issues: List[str]) -> List[str]:
    """Primary action first, then the follow-ups in the brief's fixed order.

    `verify_refund_completion` is tied to the full-refund branches: the worked
    example in the brief is a late_delivery_seller case with a non-zero freight
    refund and it does *not* carry that action, so 'refund > 0' cannot be the
    trigger. `verify_payment_allocation` is suppressed under
    valid_split_payment because the primary action already explains the split.
    """
    actions = [PRIMARY_ACTION[primary_issue]]

    if primary_issue == LATE_DELIVERY_SELLER:
        actions.append("review_seller_handoff")
    elif primary_issue == LATE_DELIVERY_LOGISTICS:
        actions.append("review_carrier_delay")

    if primary_issue in (CANCELED_ORDER_PAID, UNAVAILABLE_ORDER_PAID):
        actions.append("verify_refund_completion")

    if "multi_seller_order" in secondary_issues:
        actions.append("coordinate_multi_seller_case")

    if "split_payment" in secondary_issues and primary_issue != VALID_SPLIT_PAYMENT:
        actions.append("verify_payment_allocation")

    return actions


def build_evidence_ids(
    order_id: str,
    item_ids: List[str],
    payment_ids: List[str],
    responsible_parties: List[dict],
    root_cause_code: str,
) -> List[str]:
    """Only IDs reconstructible straight from the CSVs; anything else counts as
    a false positive. Seller evidence appears only when a seller is the party
    actually held responsible."""
    evidence = [f"order:{order_id}"]
    evidence.extend(f"item:{item_id}" for item_id in item_ids)
    evidence.extend(f"payment:{payment_id}" for payment_id in payment_ids)
    evidence.extend(
        f"seller:{party['party_id']}"
        for party in responsible_parties
        if party["party_type"] == "seller"
    )
    evidence.append(f"policy:{root_cause_code}")
    return evidence


def apply_policy(order: dict, delivery: dict, payment: dict, counts: dict,
                 related_order_ids: List[str], late: bool) -> dict:
    primary_issue = classify_primary_issue(
        order, delivery, payment, counts["payment_count"], late
    )
    secondary_issues = derive_secondary_issues(counts, related_order_ids)
    responsible_parties = derive_responsible_parties(primary_issue, delivery)
    refund = derive_refund(primary_issue, payment)
    root_cause_code = ROOT_CAUSE[primary_issue]

    return {
        "primary_issue": primary_issue,
        "secondary_issues": secondary_issues,
        "case_status": "action_required" if refund > 0 else "no_action",
        "confidence": CONFIDENCE[primary_issue],
        "root_cause_code": root_cause_code,
        "ranked_causes": [{"cause_code": root_cause_code, "rank": 1}],
        "responsible_parties": responsible_parties,
        "recommended_refund_brl": refund,
        "resolution_actions": derive_actions(primary_issue, secondary_issues),
    }
