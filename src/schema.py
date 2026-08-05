"""Output assembly and verification.

`build_case_output` turns the analyser results into the submission schema and
enforces the array caps; `verify_case_output` is the independent second pass
that the Verifier agent runs before anything is written to disk.
"""

from __future__ import annotations

import re
from typing import List, Optional

LIMITS = {
    "order_ids": 5,
    "item_ids": 5,
    "seller_ids": 3,
    "payment_ids": 5,
    "related_order_ids": 5,
    "product_ids": 5,
    "category_names": 5,
    "ranked_causes": 3,
    "responsible_parties": 3,
    "evidence_ids": 20,
    "resolution_actions": 5,
}

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "unsupported_late_claim",
}

SECONDARY_ISSUES = [
    "multi_item_order",
    "multi_seller_order",
    "split_payment",
    "repeat_customer",
    "multiple_categories",
]

ROOT_CAUSE_CODES = {
    "SELLER_HANDOFF_AFTER_LIMIT",
    "CARRIER_DELIVERED_AFTER_ESTIMATE",
    "ORDER_CANCELED_AFTER_PAYMENT",
    "ORDER_UNAVAILABLE_AFTER_PAYMENT",
    "MULTIPLE_PAYMENTS_RECONCILED",
    "DELIVERY_WITHIN_ESTIMATE",
}

TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
HEX32 = r"[0-9a-f]{32}"
EVIDENCE_PATTERNS = [
    re.compile(rf"^order:{HEX32}$"),
    re.compile(rf"^item:{HEX32}:\d+$"),
    re.compile(rf"^payment:{HEX32}:\d+$"),
    re.compile(rf"^seller:{HEX32}$"),
    re.compile(r"^policy:[A-Z_]+$"),
]


def _cap(values: List, key: str) -> List:
    return values[: LIMITS[key]]


def build_case_output(case_id: str, order_id: str, delivery: dict, payment: dict,
                      customer: dict, products: dict, counts: dict, verdict: dict,
                      evidence_ids: List[str]) -> dict:
    return {
        "case_id": case_id,
        "case_assessment": {
            "primary_issue": verdict["primary_issue"],
            "secondary_issues": verdict["secondary_issues"],
            "case_status": verdict["case_status"],
            "confidence": verdict["confidence"],
        },
        "affected_entities": {
            "order_ids": _cap([order_id], "order_ids"),
            "item_ids": _cap(counts["item_ids"], "item_ids"),
            "seller_ids": _cap(counts["seller_ids"], "seller_ids"),
            "payment_ids": _cap(counts["payment_ids"], "payment_ids"),
        },
        "customer_context": {
            "customer_unique_id": customer["customer_unique_id"],
            "related_order_ids": _cap(customer["related_order_ids"], "related_order_ids"),
        },
        "product_context": {
            "product_ids": _cap(products["product_ids"], "product_ids"),
            "category_names": _cap(products["category_names"], "category_names"),
        },
        "delivery_analysis": delivery,
        "payment_reconciliation": payment,
        "root_cause_analysis": {
            "ranked_causes": _cap(verdict["ranked_causes"], "ranked_causes"),
            "responsible_parties": _cap(verdict["responsible_parties"], "responsible_parties"),
        },
        "evidence_ids": _cap(evidence_ids, "evidence_ids"),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": verdict["recommended_refund_brl"],
        },
        "resolution_actions": _cap(verdict["resolution_actions"], "resolution_actions"),
    }


# ------------------------------------------------------------------- verifier


def _check_timestamp(errors: List[str], label: str, value: Optional[str]) -> None:
    if value is not None and not TS_PATTERN.match(value):
        errors.append(f"{label}: bad timestamp format {value!r}")


def _check_money(errors: List[str], label: str, value) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{label}: not numeric ({value!r})")
    elif round(value, 2) != value:
        errors.append(f"{label}: more than 2 decimals ({value!r})")


def verify_case_output(doc: dict, case_id: str, store) -> List[str]:
    """Return a list of problems; empty means the document is safe to write."""
    errors: List[str] = []

    if doc.get("case_id") != case_id:
        errors.append(f"case_id mismatch: {doc.get('case_id')!r} != {case_id!r}")

    assessment = doc["case_assessment"]
    if assessment["primary_issue"] not in PRIMARY_ISSUES:
        errors.append(f"unknown primary_issue {assessment['primary_issue']!r}")
    if assessment["case_status"] not in ("action_required", "no_action"):
        errors.append(f"unknown case_status {assessment['case_status']!r}")
    if not 0.0 <= assessment["confidence"] <= 1.0:
        errors.append(f"confidence out of range: {assessment['confidence']}")

    seen = [s for s in assessment["secondary_issues"] if s in SECONDARY_ISSUES]
    if seen != sorted(seen, key=SECONDARY_ISSUES.index):
        errors.append("secondary_issues are not in policy order")
    if len(set(assessment["secondary_issues"])) != len(assessment["secondary_issues"]):
        errors.append("duplicate secondary_issues")
    for issue in assessment["secondary_issues"]:
        if issue not in SECONDARY_ISSUES:
            errors.append(f"unknown secondary_issue {issue!r}")

    entities = doc["affected_entities"]
    order_ids = entities["order_ids"]
    if len(order_ids) != 1 or store.get_order(order_ids[0]) is None:
        errors.append(f"affected order_ids not resolvable: {order_ids}")
    order_id = order_ids[0]

    known_items = {f"{i['order_id']}:{i['order_item_id']}" for i in store.get_items(order_id)}
    known_payments = {
        f"{p['order_id']}:{p['payment_sequential']}" for p in store.get_payments(order_id)
    }
    known_sellers = {i["seller_id"] for i in store.get_items(order_id)}

    for item_id in entities["item_ids"]:
        if item_id not in known_items:
            errors.append(f"item_id not in CSV: {item_id}")
    for payment_id in entities["payment_ids"]:
        if payment_id not in known_payments:
            errors.append(f"payment_id not in CSV: {payment_id}")
    for seller_id in entities["seller_ids"]:
        if seller_id not in known_sellers:
            errors.append(f"seller_id not on this order: {seller_id}")

    related = doc["customer_context"]["related_order_ids"]
    if order_id in related:
        errors.append("claimed order leaked into related_order_ids")
    for related_id in related:
        if store.get_order(related_id) is None:
            errors.append(f"related order not in CSV: {related_id}")

    delivery = doc["delivery_analysis"]
    for key in ("delivered_at", "estimated_delivery_at", "carrier_handoff_at"):
        _check_timestamp(errors, f"delivery_analysis.{key}", delivery[key])
    for entry in delivery["seller_handoff_analysis"]:
        _check_timestamp(errors, "seller_handoff_analysis.shipping_limit_at",
                         entry["shipping_limit_at"])
        if entry["seller_id"] not in known_sellers:
            errors.append(f"handoff analysis for unknown seller: {entry['seller_id']}")
        if entry["late_handoff"] != (
            entry["handoff_variance_hours"] is not None and entry["handoff_variance_hours"] > 0
        ):
            errors.append(f"late_handoff inconsistent for seller {entry['seller_id']}")

    payment = doc["payment_reconciliation"]
    for key in ("item_total_brl", "freight_total_brl", "expected_total_brl",
                "payment_total_brl", "difference_brl"):
        _check_money(errors, f"payment_reconciliation.{key}", payment[key])
    if not store.get_items(order_id):
        for key in ("expected_total_brl", "difference_brl", "reconciled"):
            if payment[key] is not None:
                errors.append(f"payment_reconciliation.{key} must be null for item-less order")
        if doc["product_context"]["product_ids"] or entities["item_ids"] \
                or entities["seller_ids"] or delivery["seller_handoff_analysis"]:
            errors.append("item-less order must have empty item/seller/product arrays")

    for cause in doc["root_cause_analysis"]["ranked_causes"]:
        if cause["cause_code"] not in ROOT_CAUSE_CODES:
            errors.append(f"unknown cause_code {cause['cause_code']!r}")
    for party in doc["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in known_sellers:
            errors.append(f"responsible seller not on order: {party['party_id']}")

    for evidence in doc["evidence_ids"]:
        if not any(pattern.match(evidence) for pattern in EVIDENCE_PATTERNS):
            errors.append(f"malformed evidence id: {evidence}")
            continue
        kind, _, rest = evidence.partition(":")
        if kind == "order" and store.get_order(rest) is None:
            errors.append(f"evidence order not in CSV: {evidence}")
        elif kind == "item" and rest not in known_items:
            errors.append(f"evidence item not in CSV: {evidence}")
        elif kind == "payment" and rest not in known_payments:
            errors.append(f"evidence payment not in CSV: {evidence}")
        elif kind == "seller" and rest not in known_sellers:
            errors.append(f"evidence seller not on order: {evidence}")
        elif kind == "policy" and rest not in ROOT_CAUSE_CODES:
            errors.append(f"evidence policy code unknown: {evidence}")
    if len(set(doc["evidence_ids"])) != len(doc["evidence_ids"]):
        errors.append("duplicate evidence_ids")

    refund = doc["financial_resolution"]["recommended_refund_brl"]
    _check_money(errors, "recommended_refund_brl", refund)
    expected_status = "action_required" if refund > 0 else "no_action"
    if assessment["case_status"] != expected_status:
        errors.append(f"case_status {assessment['case_status']!r} disagrees with refund {refund}")

    if len(doc["resolution_actions"]) != len(set(doc["resolution_actions"])):
        errors.append("duplicate resolution_actions")

    for key, limit in LIMITS.items():
        values = _locate(doc, key)
        if values is not None and len(values) > limit:
            errors.append(f"{key} exceeds limit {limit} ({len(values)})")

    return errors


def _locate(doc: dict, key: str):
    for container in (doc["affected_entities"], doc["customer_context"],
                      doc["product_context"], doc["root_cause_analysis"], doc):
        if key in container:
            return container[key]
    return None
