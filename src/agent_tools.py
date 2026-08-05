"""Tools exposed to the agents, plus the per-agent access matrix.

Every number in the final submission originates here, never from model prose:
a <=10B model cannot be trusted to subtract two timestamps or sum BRL to the
cent. The agents decide *which* tool to call and *what the result means*; the
arithmetic stays in `src/analysis.py`.

The access matrix is the enforcement point for the brief's separation-of-duty
requirement — the Delivery agent physically cannot read payment rows.
"""

from __future__ import annotations

from typing import Dict, List

from . import analysis

# ----------------------------------------------------------------- tool specs

TOOL_SPECS: Dict[str, dict] = {
    "lookup_customer_history": {
        "type": "function",
        "function": {
            "name": "lookup_customer_history",
            "description": (
                "Resolve the shopper behind an order and list their other orders. "
                "customer_id is per-order in Olist, so repeat purchases are only "
                "visible through customer_unique_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    "lookup_order_items": {
        "type": "function",
        "function": {
            "name": "lookup_order_items",
            "description": (
                "Return the item rows of an order with their sellers, products and "
                "categories, plus item/seller/category counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    "reconcile_order_payments": {
        "type": "function",
        "function": {
            "name": "reconcile_order_payments",
            "description": (
                "Sum the payment rows of an order and compare against item price + "
                "freight. Returns expected/paid/difference in BRL and whether the "
                "order reconciles within the 0.10 BRL tolerance."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    "analyze_order_delivery": {
        "type": "function",
        "function": {
            "name": "analyze_order_delivery",
            "description": (
                "Compare delivery against the estimate and each seller's shipping "
                "limit. Returns delivery_variance_hours, per-seller handoff variance "
                "and which sellers handed off to the carrier late."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
}

# Which agent may call which tool. Anything not listed is denied at runtime.
AGENT_TOOL_ACCESS: Dict[str, List[str]] = {
    "coordinator": [],
    "customer": ["lookup_customer_history"],
    "order_product": ["lookup_order_items"],
    "payment": ["reconcile_order_payments"],
    "delivery": ["analyze_order_delivery"],
    "policy": [],
    "verifier": [],
}


class ToolAccessError(RuntimeError):
    pass


class ToolRegistry:
    """Binds the tool implementations to a loaded store and enforces access."""

    def __init__(self, store):
        self.store = store

    def specs_for(self, agent: str) -> List[dict]:
        return [TOOL_SPECS[name] for name in AGENT_TOOL_ACCESS.get(agent, [])]

    def call(self, agent: str, name: str, arguments: dict) -> dict:
        if name not in AGENT_TOOL_ACCESS.get(agent, []):
            raise ToolAccessError(f"agent {agent!r} may not call tool {name!r}")
        order_id = (arguments or {}).get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError(f"{name}: missing order_id")
        return getattr(self, f"_{name}")(order_id)

    # ------------------------------------------------------------ implementations

    def _lookup_customer_history(self, order_id: str) -> dict:
        order = self._order(order_id)
        customer = analysis.resolve_customer(self.store, order)
        return {
            "customer_unique_id": customer["customer_unique_id"],
            "related_order_ids": customer["related_order_ids"],
            "related_order_count": len(customer["related_order_ids"]),
            "repeat_customer": bool(customer["related_order_ids"]),
        }

    def _lookup_order_items(self, order_id: str) -> dict:
        order = self._order(order_id)
        items = self.store.get_items(order_id)
        products = analysis.describe_products(self.store, items)
        counts = analysis.summarize_order(items, [], products["category_names"])
        return {
            "order_status": order["order_status"],
            "item_ids": counts["item_ids"],
            "seller_ids": counts["seller_ids"],
            "product_ids": products["product_ids"],
            "category_names": products["category_names"],
            "item_count": counts["item_count"],
            "seller_count": counts["seller_count"],
            "category_count": counts["category_count"],
        }

    def _reconcile_order_payments(self, order_id: str) -> dict:
        self._order(order_id)
        items = self.store.get_items(order_id)
        payments = self.store.get_payments(order_id)
        result = analysis.reconcile_payments(items, payments)
        counts = analysis.summarize_order(items, payments, [])
        result["payment_ids"] = counts["payment_ids"]
        result["payment_count"] = counts["payment_count"]
        return result

    def _analyze_order_delivery(self, order_id: str) -> dict:
        order = self._order(order_id)
        items = self.store.get_items(order_id)
        delivery = analysis.analyze_delivery(order, items)
        result = dict(delivery)
        result["order_status"] = order["order_status"]
        result["late_delivery"] = analysis.is_late_delivery(delivery)
        return result

    def _order(self, order_id: str) -> dict:
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"order_id {order_id} not present in orders CSV")
        return order
