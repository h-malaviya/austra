"""Deterministic supplier verification and purchase order review."""

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from austra.store import Store, _id, _now

MAX_QUANTITY_TONNES = Decimal(3)
MAX_UNIT_PRICE_USD = Decimal(7000)
MAX_TOTAL_USD = Decimal(21000)
MISSING_TERMS = frozenset(
    {"", "unknown", "tbd", "to be determined", "not specified", "n/a", "none"}
)


def _money(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid numeric amount") from error
    if not number.is_finite() or number <= 0:
        raise ValueError("Amount must be positive and finite")
    return number


def save_purchasing_drafts(
    connection: sqlite3.Connection, case_id: str, plan: dict[str, Any]
) -> None:
    for table in ("supplier_proposals", "purchase_orders"):
        connection.execute(
            f"UPDATE {table} SET status = 'superseded' WHERE case_id = ? AND status = 'proposed'",
            (case_id,),
        )
    for draft in plan.get("supplier_drafts", []):
        connection.execute(
            "INSERT INTO supplier_proposals "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["name"],
                draft["email"].lower(),
                draft["material"].strip().casefold(),
                json.dumps(draft["source_document_ids"]),
                draft["reason"],
                _now(),
            ),
        )
    for draft in plan.get("po_drafts", []):
        quantity = _money(draft["quantity_tonnes"])
        price = _money(draft["unit_price_usd"])
        connection.execute(
            "INSERT INTO purchase_orders "
            "(id, case_id, supplier_id, material, grade, quantity_tonnes, unit_price_usd, "
            "total_usd, payment_terms, delivery_terms, freight_terms, tax_terms, reason, "
            "status, created_at, reviewed_at, reviewed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["supplier_id"],
                draft["material"].strip().casefold(),
                draft["grade"],
                str(quantity),
                str(price),
                str(quantity * price),
                draft["payment_terms"],
                draft["delivery_terms"],
                draft["freight_terms"],
                draft["tax_terms"],
                draft["reason"],
                _now(),
            ),
        )


class Purchasing:
    def __init__(self, store: Store) -> None:
        self.store = store

    def for_case(self, case_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.store._connect() as connection:
            result = {}
            for key, table in (
                ("supplier_drafts", "supplier_proposals"),
                ("purchase_orders", "purchase_orders"),
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE case_id = ? ORDER BY created_at",
                    (case_id,),
                )
                result[key] = [dict(row) for row in rows]
            for draft in result["supplier_drafts"]:
                draft["source_document_ids"] = json.loads(draft.pop("source_document_ids_json"))
            return result

    def verified_suppliers(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, name, email, material, verified_at FROM suppliers "
                    "WHERE status = 'verified' ORDER BY name LIMIT 100"
                )
            ]

    def review_supplier(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM supplier_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "proposed":
                raise ValueError("Proposal has already been reviewed")
            if decision == "approved":
                document_ids = json.loads(row["source_document_ids_json"])
                if document_ids:
                    found = connection.execute(
                        f"SELECT id FROM documents WHERE id IN ({','.join('?' for _ in document_ids)})",
                        document_ids,
                    ).fetchall()
                    if len(found) != len(set(document_ids)):
                        raise ValueError("Supplier references an unknown document")
                if connection.execute(
                    "SELECT id FROM suppliers WHERE email = ?", (row["email"],)
                ).fetchone():
                    raise ValueError("Supplier email already exists")
                connection.execute(
                    "INSERT INTO suppliers VALUES (?, ?, ?, ?, ?, 'verified', ?, ?)",
                    (
                        _id(),
                        row["name"],
                        row["email"],
                        row["material"],
                        proposal_id,
                        _now(),
                        manager,
                    ),
                )
            connection.execute(
                "UPDATE supplier_proposals SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, proposal_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "supplier_reviewed",
                {"proposal_id": proposal_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM supplier_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
            )

    def review_po(self, po_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM purchase_orders WHERE id = ?", (po_id,)
            ).fetchone()
            if row is None:
                raise KeyError(po_id)
            if row["status"] != "proposed":
                raise ValueError("PO has already been reviewed")
            if decision == "approved":
                supplier = self._check_po(connection, row)
                from austra.email_outbox import prepare_po_email

                prepare_po_email(connection, row, supplier)
            connection.execute(
                "UPDATE purchase_orders SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, po_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "po_reviewed",
                {"po_id": po_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM purchase_orders WHERE id = ?", (po_id,)
                ).fetchone()
            )

    def _check_po(self, connection: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        supplier = connection.execute(
            "SELECT * FROM suppliers WHERE id = ? AND status = 'verified'",
            (row["supplier_id"],),
        ).fetchone()
        if supplier is None or supplier["material"].casefold() != row["material"].casefold():
            raise ValueError("A verified supplier for this material is required")
        quantity = _money(row["quantity_tonnes"])
        price = _money(row["unit_price_usd"])
        if quantity > MAX_QUANTITY_TONNES or price > MAX_UNIT_PRICE_USD:
            raise ValueError("PO exceeds the 3 tonne or USD 7,000 per tonne limit")
        if quantity * price > MAX_TOTAL_USD:
            raise ValueError("PO exceeds the USD 21,000 material value limit")
        if any(
            row[key].strip().casefold() in MISSING_TERMS
            for key in ("grade", "payment_terms", "delivery_terms", "freight_terms", "tax_terms")
        ):
            raise ValueError("Grade, payment, delivery, freight, and tax terms must be complete")
        previous = connection.execute(
            "SELECT quantity_tonnes, total_usd FROM purchase_orders "
            "WHERE case_id = ? AND lower(material) = ? AND status = 'approved'",
            (row["case_id"], row["material"]),
        ).fetchall()
        cumulative_quantity = quantity + sum(
            (Decimal(item["quantity_tonnes"]) for item in previous), Decimal(0)
        )
        cumulative_value = quantity * price + sum(
            (Decimal(item["total_usd"]) for item in previous), Decimal(0)
        )
        if cumulative_quantity > MAX_QUANTITY_TONNES or cumulative_value > MAX_TOTAL_USD:
            raise ValueError("Combined POs for this case exceed the approval limit")
        return supplier
