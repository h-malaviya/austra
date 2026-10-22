"""Verified stock receipts and sales reservations in metric tonnes."""

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from austra.store import Store, _id, _now

MISSING_DETAILS = frozenset({"", "unknown", "tbd", "not specified"})


def _tonnes(value: object) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid stock quantity") from error
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Stock quantity must be positive and finite")
    return quantity


def save_inventory_drafts(
    connection: sqlite3.Connection, case_id: str, plan: dict[str, Any]
) -> None:
    for table in ("stock_receipts", "sales_orders"):
        connection.execute(
            f"UPDATE {table} SET status = 'superseded' WHERE case_id = ? AND status = 'proposed'",
            (case_id,),
        )
    for draft in plan.get("stock_receipts", []):
        connection.execute(
            "INSERT INTO stock_receipts "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["item"].strip().casefold(),
                draft["item_kind"],
                draft["lot_code"].strip(),
                draft["grade"],
                draft["location"],
                str(_tonnes(draft["quantity_tonnes"])),
                draft["reason"],
                _now(),
            ),
        )
    for draft in plan.get("sales_orders", []):
        connection.execute(
            "INSERT INTO sales_orders VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["customer"],
                draft["item"].strip().casefold(),
                str(_tonnes(draft["quantity_tonnes"])),
                draft["reason"],
                _now(),
            ),
        )


class Inventory:
    def __init__(self, store: Store) -> None:
        self.store = store

    def for_case(self, case_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.store._connect() as connection:
            result = {}
            for key, table in (
                ("stock_receipts", "stock_receipts"),
                ("sales_orders", "sales_orders"),
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE case_id = ? ORDER BY created_at", (case_id,)
                )
                result[key] = [dict(row) for row in rows]
            for order in result["sales_orders"]:
                order["available_tonnes"] = str(self._available(connection, order["item"]))
            return result

    def stock_snapshot(self) -> list[dict[str, str]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT item, item_kind FROM stock_receipts "
                "WHERE status = 'approved' ORDER BY item"
            )
            snapshot = []
            for row in rows:
                receipts = connection.execute(
                    "SELECT quantity_tonnes FROM stock_receipts "
                    "WHERE item = ? AND item_kind = ? AND status = 'approved'",
                    (row["item"], row["item_kind"]),
                )
                on_hand = sum((Decimal(item["quantity_tonnes"]) for item in receipts), Decimal(0))
                available = (
                    self._available(connection, row["item"])
                    if row["item_kind"] == "finished_product"
                    else on_hand
                )
                snapshot.append(
                    {
                        "item": row["item"],
                        "item_kind": row["item_kind"],
                        "on_hand_tonnes": str(on_hand),
                        "available_tonnes": str(available),
                    }
                )
            return snapshot

    def _available(self, connection: sqlite3.Connection, item: str) -> Decimal:
        receipts = connection.execute(
            "SELECT quantity_tonnes FROM stock_receipts WHERE item = ? "
            "AND item_kind = 'finished_product' AND status = 'approved'",
            (item,),
        )
        orders = connection.execute(
            "SELECT quantity_tonnes FROM sales_orders WHERE item = ? AND status = 'approved'",
            (item,),
        )
        received = sum((Decimal(row["quantity_tonnes"]) for row in receipts), Decimal(0))
        reserved = sum((Decimal(row["quantity_tonnes"]) for row in orders), Decimal(0))
        return received - reserved

    def review_receipt(self, receipt_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM stock_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(receipt_id)
            if row["status"] != "proposed":
                raise ValueError("Stock receipt has already been reviewed")
            if decision == "approved":
                if any(
                    row[key].strip().casefold() in MISSING_DETAILS
                    for key in ("lot_code", "grade", "location")
                ):
                    raise ValueError("Lot, grade, and location are required")
                duplicate = connection.execute(
                    "SELECT id FROM stock_receipts WHERE item = ? AND lot_code = ? "
                    "AND status = 'approved'",
                    (row["item"], row["lot_code"]),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("This item and lot have already been received")
            connection.execute(
                "UPDATE stock_receipts SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, receipt_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "stock_receipt_reviewed",
                {"receipt_id": receipt_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM stock_receipts WHERE id = ?", (receipt_id,)
                ).fetchone()
            )

    def review_sales_order(self, order_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sales_orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(order_id)
            if row["status"] != "proposed":
                raise ValueError("Sales order has already been reviewed")
            if decision == "approved" and self._available(connection, row["item"]) < _tonnes(
                row["quantity_tonnes"]
            ):
                raise ValueError(
                    "Insufficient verified finished stock; manager must resolve shortage"
                )
            connection.execute(
                "UPDATE sales_orders SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                (decision, _now(), manager, order_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "sales_order_reviewed",
                {"order_id": order_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM sales_orders WHERE id = ?", (order_id,)
                ).fetchone()
            )
