"""Olist CSV access layer.

Loads the five datasets the policy actually needs and exposes lookups keyed by
order. Timestamps are kept as raw CSV strings (`YYYY-MM-DD HH:MM:SS`) so that
output values never drift from the source; parsing happens only inside the
analysis helpers. Money columns stay as strings too and are converted to
Decimal at use time to avoid float rounding noise.

`olist_geolocation_dataset.csv` (62 MB) is deliberately not loaded: no field in
the output schema depends on it.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional

# Raise the field-size cap: order_reviews carries long free-text comments.
csv.field_size_limit(10_000_000)

ORDERS_CSV = "olist_orders_dataset.csv"
ITEMS_CSV = "olist_order_items_dataset.csv"
PAYMENTS_CSV = "olist_order_payments_dataset.csv"
CUSTOMERS_CSV = "olist_customers_dataset.csv"
PRODUCTS_CSV = "olist_products_dataset.csv"


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_rows(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class OlistStore:
    """In-memory index over the Olist CSVs, built once per run."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir

        self.orders: Dict[str, dict] = {}
        self.items_by_order: Dict[str, List[dict]] = {}
        self.payments_by_order: Dict[str, List[dict]] = {}
        self.customer_unique_by_customer: Dict[str, str] = {}
        self.orders_by_customer_unique: Dict[str, List[str]] = {}
        self.category_by_product: Dict[str, Optional[str]] = {}

        self._load_customers()
        self._load_orders()
        self._load_items()
        self._load_payments()
        self._load_products()

    # ------------------------------------------------------------------ load

    def _load_customers(self) -> None:
        for row in _read_rows(os.path.join(self.data_dir, CUSTOMERS_CSV)):
            self.customer_unique_by_customer[row["customer_id"]] = row["customer_unique_id"]

    def _load_orders(self) -> None:
        # Rows are kept in CSV order so that `related_order_ids` has a stable,
        # source-derived ordering rather than depending on dict iteration luck.
        for row in _read_rows(os.path.join(self.data_dir, ORDERS_CSV)):
            order_id = row["order_id"]
            order = {
                "order_id": order_id,
                "customer_id": row["customer_id"],
                "order_status": row["order_status"],
                "order_purchase_timestamp": _blank_to_none(row["order_purchase_timestamp"]),
                "order_approved_at": _blank_to_none(row["order_approved_at"]),
                "order_delivered_carrier_date": _blank_to_none(row["order_delivered_carrier_date"]),
                "order_delivered_customer_date": _blank_to_none(row["order_delivered_customer_date"]),
                "order_estimated_delivery_date": _blank_to_none(row["order_estimated_delivery_date"]),
            }
            self.orders[order_id] = order

            unique_id = self.customer_unique_by_customer.get(order["customer_id"])
            if unique_id is not None:
                self.orders_by_customer_unique.setdefault(unique_id, []).append(order_id)

    def _load_items(self) -> None:
        for row in _read_rows(os.path.join(self.data_dir, ITEMS_CSV)):
            self.items_by_order.setdefault(row["order_id"], []).append(
                {
                    "order_id": row["order_id"],
                    "order_item_id": int(row["order_item_id"]),
                    "product_id": row["product_id"],
                    "seller_id": row["seller_id"],
                    "shipping_limit_date": _blank_to_none(row["shipping_limit_date"]),
                    "price": row["price"],
                    "freight_value": row["freight_value"],
                }
            )
        for rows in self.items_by_order.values():
            rows.sort(key=lambda r: r["order_item_id"])

    def _load_payments(self) -> None:
        for row in _read_rows(os.path.join(self.data_dir, PAYMENTS_CSV)):
            self.payments_by_order.setdefault(row["order_id"], []).append(
                {
                    "order_id": row["order_id"],
                    "payment_sequential": int(row["payment_sequential"]),
                    "payment_type": row["payment_type"],
                    "payment_installments": int(row["payment_installments"]),
                    "payment_value": row["payment_value"],
                }
            )
        for rows in self.payments_by_order.values():
            rows.sort(key=lambda r: r["payment_sequential"])

    def _load_products(self) -> None:
        for row in _read_rows(os.path.join(self.data_dir, PRODUCTS_CSV)):
            self.category_by_product[row["product_id"]] = _blank_to_none(
                row.get("product_category_name")
            )

    # ---------------------------------------------------------------- lookups

    def get_order(self, order_id: str) -> Optional[dict]:
        return self.orders.get(order_id)

    def get_items(self, order_id: str) -> List[dict]:
        return self.items_by_order.get(order_id, [])

    def get_payments(self, order_id: str) -> List[dict]:
        return self.payments_by_order.get(order_id, [])

    def get_customer_unique_id(self, customer_id: str) -> Optional[str]:
        return self.customer_unique_by_customer.get(customer_id)

    def get_related_order_ids(self, customer_unique_id: str, exclude_order_id: str) -> List[str]:
        """Other orders placed by the same shopper, in CSV row order."""
        return [
            oid
            for oid in self.orders_by_customer_unique.get(customer_unique_id, [])
            if oid != exclude_order_id
        ]

    def get_category(self, product_id: str) -> Optional[str]:
        return self.category_by_product.get(product_id)
