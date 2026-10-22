import pytest

from austra.agent import CasePlan
from austra.inventory import Inventory
from austra.store import Store


def plan(receipts=None, orders=None):
    return CasePlan.model_validate(
        {
            "summary": "Stock and sales review",
            "category": "inventory",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "stock_receipts": receipts or [],
            "sales_orders": orders or [],
        }
    )


def receipt(item, kind, lot, quantity):
    return {
        "item": item,
        "item_kind": kind,
        "lot_code": lot,
        "grade": "high purity",
        "location": "Ahmedabad",
        "quantity_tonnes": quantity,
        "reason": "Measured receipt",
    }


def sale(item, quantity):
    return {
        "customer": "Solar Customer",
        "item": item,
        "quantity_tonnes": quantity,
        "reason": "Customer request",
    }


def test_raw_stock_cannot_satisfy_finished_sales_order(tmp_path):
    store = Store(tmp_path / "business.db")
    inventory = Inventory(store)
    case = store.create_case("Sales order", "ui", "Owner")
    store.save_plan(
        case["id"],
        plan(receipts=[receipt("quartz", "raw_material", "Q-1", 0.5)]).model_dump(),
        [],
    )
    receipt_id = inventory.for_case(case["id"])["stock_receipts"][0]["id"]
    inventory.review_receipt(receipt_id, "approved", "Manager")
    store.save_plan(case["id"], plan(orders=[sale("polysilicon", 1)]).model_dump(), [])
    order_id = inventory.for_case(case["id"])["sales_orders"][0]["id"]

    with pytest.raises(ValueError, match="Insufficient verified finished stock"):
        inventory.review_sales_order(order_id, "approved", "Manager")

    assert inventory.stock_snapshot()[0]["available_tonnes"] == "0.5"
    assert inventory.for_case(case["id"])["sales_orders"][0]["status"] == "proposed"


def test_finished_stock_is_reserved_once_and_duplicate_lot_rejected(tmp_path):
    store = Store(tmp_path / "business.db")
    inventory = Inventory(store)
    case = store.create_case("Sales order", "ui", "Owner")
    store.save_plan(
        case["id"],
        plan(receipts=[receipt("polysilicon", "finished_product", "P-1", 1.5)]).model_dump(),
        [],
    )
    receipt_id = inventory.for_case(case["id"])["stock_receipts"][0]["id"]
    inventory.review_receipt(receipt_id, "approved", "Manager")

    store.save_plan(case["id"], plan(orders=[sale("polysilicon", 1)]).model_dump(), [])
    order_id = inventory.for_case(case["id"])["sales_orders"][0]["id"]
    inventory.review_sales_order(order_id, "approved", "Manager")

    assert inventory.stock_snapshot()[0]["available_tonnes"] == "0.5"
    store.save_plan(case["id"], plan(orders=[sale("polysilicon", 1)]).model_dump(), [])
    second_id = inventory.for_case(case["id"])["sales_orders"][-1]["id"]
    with pytest.raises(ValueError, match="Insufficient"):
        inventory.review_sales_order(second_id, "approved", "Manager")

    store.save_plan(
        case["id"],
        plan(receipts=[receipt("polysilicon", "finished_product", "P-1", 1)]).model_dump(),
        [],
    )
    duplicate_id = inventory.for_case(case["id"])["stock_receipts"][-1]["id"]
    with pytest.raises(ValueError, match="already been received"):
        inventory.review_receipt(duplicate_id, "approved", "Manager")
