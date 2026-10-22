import sqlite3

import pytest

from austra.agent import CasePlan
from austra.purchasing import Purchasing
from austra.store import Store


def plan(suppliers=None, orders=None):
    return CasePlan.model_validate(
        {
            "summary": "Quartz purchase",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "supplier_drafts": suppliers or [],
            "po_drafts": orders or [],
        }
    )


def order(supplier_id, quantity=2, price=6000, payment="Net 30", delivery="Ahmedabad"):
    return {
        "supplier_id": supplier_id,
        "material": "quartz",
        "grade": "high purity quartz",
        "quantity_tonnes": quantity,
        "unit_price_usd": price,
        "payment_terms": payment,
        "delivery_terms": delivery,
        "freight_terms": "Buyer pays freight",
        "tax_terms": "GST excluded",
        "reason": "Case request",
    }


def test_verified_supplier_and_po_limits(tmp_path):
    store = Store(tmp_path / "business.db")
    purchasing = Purchasing(store)
    case = store.create_case("Buy quartz", "ui", "Owner")
    store.save_plan(
        case["id"],
        plan(
            suppliers=[
                {
                    "name": "Quartz Supplier",
                    "email": "sales@example.com",
                    "material": "quartz",
                    "source_document_ids": [],
                    "reason": "Owner named supplier",
                }
            ]
        ).model_dump(),
        [],
    )
    proposal_id = purchasing.for_case(case["id"])["supplier_drafts"][0]["id"]
    assert purchasing.verified_suppliers() == []

    purchasing.review_supplier(proposal_id, "approved", "Manager")
    supplier_id = purchasing.verified_suppliers()[0]["id"]
    store.save_plan(case["id"], plan(orders=[order(supplier_id)]).model_dump(), [])
    po_id = purchasing.for_case(case["id"])["purchase_orders"][0]["id"]

    approved = purchasing.review_po(po_id, "approved", "Manager")
    assert approved["status"] == "approved"
    assert approved["total_usd"] == "12000.00"

    store.save_plan(case["id"], plan(orders=[order(supplier_id)]).model_dump(), [])
    second_id = purchasing.for_case(case["id"])["purchase_orders"][-1]["id"]
    with pytest.raises(ValueError, match="Combined POs"):
        purchasing.review_po(second_id, "approved", "Manager")

    store.save_plan(case["id"], plan(orders=[order(supplier_id, price=7100)]).model_dump(), [])
    expensive_id = purchasing.for_case(case["id"])["purchase_orders"][-1]["id"]
    with pytest.raises(ValueError, match="USD 7,000"):
        purchasing.review_po(expensive_id, "approved", "Manager")

    incomplete = order(supplier_id)
    incomplete["tax_terms"] = "TBD"
    store.save_plan(case["id"], plan(orders=[incomplete]).model_dump(), [])
    incomplete_id = purchasing.for_case(case["id"])["purchase_orders"][-1]["id"]
    with pytest.raises(ValueError, match="must be complete"):
        purchasing.review_po(incomplete_id, "approved", "Manager")


def test_po_requires_verified_supplier_and_complete_terms(tmp_path):
    store = Store(tmp_path / "business.db")
    purchasing = Purchasing(store)
    case = store.create_case("Buy quartz", "ui", "Owner")
    store.save_plan(case["id"], plan(orders=[order("invented")]).model_dump(), [])
    po_id = purchasing.for_case(case["id"])["purchase_orders"][0]["id"]

    with pytest.raises(ValueError, match="verified supplier"):
        purchasing.review_po(po_id, "approved", "Manager")
    assert purchasing.for_case(case["id"])["purchase_orders"][0]["status"] == "proposed"


def test_nonfinite_order_value_is_rejected_before_storage(tmp_path):
    store = Store(tmp_path / "business.db")
    case = store.create_case("Buy quartz", "ui", "Owner")

    with pytest.raises(ValueError, match="finite"):
        store.save_plan(
            case["id"], plan(orders=[order("none", price=float("inf"))]).model_dump(), []
        )

    assert store.get_case(case["id"])["status"] == "new"


def test_existing_po_table_receives_required_term_columns(tmp_path):
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE purchase_orders (id TEXT PRIMARY KEY, case_id TEXT, supplier_id TEXT, "
            "material TEXT, quantity_tonnes TEXT, unit_price_usd TEXT, total_usd TEXT, "
            "payment_terms TEXT, delivery_terms TEXT, reason TEXT, status TEXT, "
            "created_at TEXT, reviewed_at TEXT, reviewed_by TEXT)"
        )

    store = Store(path)
    with store._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(purchase_orders)")}
        migration = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = '20261022_po_terms_v2'"
        ).fetchone()

    assert {"grade", "freight_terms", "tax_terms"} <= columns
    assert migration is not None
