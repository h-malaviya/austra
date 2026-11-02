import io
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from austra import web
from austra.agent import AgentReply
from austra.arithmetic import calculate_expression
from austra.files import Files
from austra.material_plan import (
    build_material_plan,
    is_material_plan_followup,
    is_material_plan_request,
    material_plan_additions,
    unsupported_allocation_request,
)
from austra.tools import ReadTools
from austra.web import create_app


def _record(root, schema, identity, values):
    folder = root / "records" / schema
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "id": identity,
        "schema": schema,
        "values": values,
        "approved_at": "2026-10-28T00:00:00+00:00",
    }
    (folder / f"{identity}.json").write_text(json.dumps(record), encoding="utf-8")


def _workspace(root):
    Files(root)
    policy = root / "approved" / "policy" / "raw_material_wastage_allowance"
    policy.mkdir(parents=True)
    (policy / "v1.md").write_text(
        "# Raw material wastage allowance\nSpecific multiplier to use in calculations: **1.05**",
        encoding="utf-8",
    )
    for index, quantity in enumerate((10000, 14000, 18000), 1):
        _record(
            root,
            "customer_sales_order_request",
            f"order{index}",
            {
                "order_id": f"SO-{index}",
                "order_status": "received_pending_feasibility_review",
                "ordered_kg": quantity,
                "grade": "solar",
                "form": "chunks",
                "minimum_silicon_purity_percent": 99.9999,
                "chunk_size_min_mm": 10,
                "chunk_size_max_mm": 80,
                "requested_final_delivery_date": f"2026-11-{index:02d}",
            },
        )
    lot = {
        "lot_id": "FG-1",
        "product_grade": "solar",
        "form": "polysilicon_chunks",
        "silicon_purity_percent": 99.9999,
        "chunk_size_min_mm": 10,
        "chunk_size_max_mm": 80,
        "on_hand_kg": 2000,
        "reserved_kg": 500,
        "qc_hold_kg": 0,
        "available_kg": 1500,
        "qc_status": "released",
        "verification_status": "verified",
        "as_of_date": "2026-10-22",
    }
    _record(root, "polysilicon_stock_lot", "live", lot)
    _record(root, "polysilicon_stock_lot", "demo", {**lot, "data_origin": "synthetic_demo"})
    _record(
        root,
        "polysilicon_stock_lot",
        "hold",
        {
            **lot,
            "lot_id": "FG-2",
            "qc_status": "qc_hold",
            "available_kg": 0,
        },
    )
    ratio_names = (
        ("Quartzite charged", "t", "2.89", "Quartzite", 80000),
        ("Charcoal charged", "t", "0.84", "Charcoal", 8000),
        ("Petroleum coke charged", "t", "0.52", "Petroleum coke", 15000),
        ("MG-Si powder needed at TCS stage", "t", "1.16", "Metallurgical silicon powder", 2500),
        ("HCl reacted to form TCS", "t", "4.22", "Hydrogen chloride (anhydrous)", 4000),
        ("H2 used in deposition", "kg", "74", "Hydrogen", 900),
    )
    for index, (name, unit, quantity, inventory_name, available) in enumerate(ratio_names):
        _record(
            root,
            "polysilicon_production_material_ratio",
            f"ratio{index}",
            {
                "material_or_stage": name,
                "unit": unit,
                "quantity_per_tonne_finished_6n": quantity,
                "basis": "charged quantity",
            },
        )
        _record(
            root,
            "raw_material_inventory_lot",
            f"raw{index}",
            {
                "lot_id": f"RM-{index}",
                "material_name": inventory_name,
                "on_hand_kg": available,
                "reserved_kg": 0,
                "qc_hold_kg": 0,
                "available_kg": available,
                "qa_status": "released",
                "verification_status": "verified",
                "as_of_date": "2026-10-22",
            },
        )


def test_material_plan_reconciles_orders_and_all_six_materials(tmp_path):
    _workspace(tmp_path)
    plan = build_material_plan(Files(tmp_path))
    assert [row["production_kg"] for row in plan["orders"]] == ["8500", "14000", "18000"]
    assert plan["excluded_demo_stock"] == 1
    assert plan["source_counts"] == {
        "open_orders": 3,
        "finished_records": 3,
        "raw_records": 6,
        "finished_lots": 2,
        "raw_lots": 6,
        "material_ratios": 6,
    }
    assert [lot["id"] for lot in plan["finished_lots"]] == ["FG-1", "FG-2"]
    assert all(lot["record_id"] for lot in plan["finished_lots"])
    assert all(lot["record_id"] for material in plan["inventory"] for lot in material["lots"])
    assert sum(Decimal(row["production_kg"]) for row in plan["orders"]) == 40500
    materials = {row["name"]: row for row in plan["materials"]}
    assert len(materials) == 6
    assert materials["quartzite"]["needed_kg"] == "122897.25"
    assert materials["quartzite"]["shortfall_kg"] == "42897.25"
    assert materials["hydrogen"]["needed_kg"] == "3146.85"
    assert materials["hydrogen chloride"]["available_kg"] == "4000"
    assert materials["metallurgical silicon powder"]["available_kg"] == "2500"
    assert all(
        sum(Decimal(part["needed_kg"]) for part in row["by_order"]) == Decimal(row["needed_kg"])
        for row in materials.values()
    )


def test_requirement_mail_draft_uses_latest_records_and_attaches_pdf(tmp_path):
    _workspace(tmp_path)
    stock_path = tmp_path / "records" / "polysilicon_stock_lot" / "live.json"
    current = json.loads(stock_path.read_text(encoding="utf-8"))
    current["values"]["reserved_kg"] = 1000
    current["values"]["available_kg"] = 1000
    stock_path.write_text(json.dumps(current), encoding="utf-8")

    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/chats",
        json={
            "body": "Draft mail with PDF of requirement attachment. I will review it. Use latest data."
        },
    )

    assert response.status_code == 201
    chat = response.json()["chat"]
    message = chat["messages"][-1]
    assert "Email draft for review" in message["body"]
    assert "[Add recipient]" in message["body"]
    assert "No email has been sent" in message["body"]
    assert "124.4145" in message["body"]
    assert "stock and open-order table" not in message["body"]
    attachment = message["attachments"][0]
    assert attachment["name"] == "material-requirements.pdf"
    pdf = client.get(f"/api/chats/{chat['id']}/attachments/{attachment['id']}")
    assert pdf.status_code == 200
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Material requirements for open sales orders" in text
    assert "SO-1" in text
    assert "124.4145" in text
    assert "ideal reaction equivalents" in text.lower()


def test_requirement_mail_draft_reports_missing_plan_without_attachment(tmp_path):
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/chats", json={"body": "Draft email with PDF of material requirements"}
    )

    assert response.status_code == 201
    message = response.json()["chat"]["messages"][-1]
    assert "Order, finished stock, or material-ratio records are missing" in message["body"]
    assert not message.get("attachments")


def test_missing_raw_material_is_unknown_without_losing_its_requirement(tmp_path):
    _workspace(tmp_path)
    (tmp_path / "records" / "raw_material_inventory_lot" / "raw0.json").unlink()

    plan = build_material_plan(Files(tmp_path))
    quartzite = next(row for row in plan["materials"] if row["name"] == "quartzite")
    assert quartzite["needed_kg"] == "122897.25"
    assert quartzite["available_kg"] is None
    assert quartzite["shortfall_kg"] is None
    assert quartzite["lot_ids"] == []


def test_no_raw_inventory_keeps_all_material_availability_unknown(tmp_path):
    _workspace(tmp_path)
    for path in (tmp_path / "records" / "raw_material_inventory_lot").glob("*.json"):
        path.unlink()

    plan = build_material_plan(Files(tmp_path))
    assert len(plan["materials"]) == 6
    assert plan["inventory"] == []
    assert all(row["available_kg"] is None for row in plan["materials"])


def test_conflicting_live_lot_blocks_plan(tmp_path):
    _workspace(tmp_path)
    _record(
        tmp_path,
        "polysilicon_stock_lot",
        "conflict",
        {
            "lot_id": "FG-1",
            "available_kg": 2000,
        },
    )
    with pytest.raises(ValueError, match="Conflicting active records"):
        build_material_plan(Files(tmp_path))


def test_missing_quality_evidence_blocks_plan(tmp_path):
    _workspace(tmp_path)
    path = tmp_path / "records" / "polysilicon_stock_lot" / "live.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["values"]["verification_status"]
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing quality or verification"):
        build_material_plan(Files(tmp_path))


def test_unknown_order_status_blocks_plan(tmp_path):
    _workspace(tmp_path)
    path = tmp_path / "records" / "customer_sales_order_request" / "order1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["values"]["order_status"] = "under_review"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="Unmapped sales order status"):
        build_material_plan(Files(tmp_path))


def test_material_answer_is_one_message_with_a2ui_and_full_fallback(tmp_path):
    _workspace(tmp_path)
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/chats",
        json={"body": "Give raw materials needed order wise and available for all orders"},
    )
    assert result.status_code == 201
    chat = result.json()["chat"]
    assert len(chat["messages"]) == 2
    answer = chat["messages"][-1]
    assert "SO-1" in answer["body"] and "hydrogen chloride" in answer["body"]
    assert "Complete record snapshot: 3 open orders" in answer["body"]
    assert "Distinct non-demo finished lot IDs: FG-1, FG-2." in answer["body"]
    assert len(answer["display_body"]) < len(answer["body"])
    surface = client.get(f"/api/ui/chats/{chat['id']}").json()["surfaces"][0]
    assert surface["kind"] == "report"
    assert surface["visual_complete"] is True
    assert surface["message_id"] == answer["id"]
    assert surface["messages"][0]["createSurface"]["catalogId"] == (
        "https://austra.local/catalogs/report.json"
    )
    charts = [
        item
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "AustraBarChart"
    ]
    assert [chart["mode"] for chart in charts] == ["stacked", "paired", "single"]
    assert charts[0]["rows"][0]["first"] == "1.5"
    assert charts[0]["rows"][0]["second"] == "8.5"
    tabs = next(
        item
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "Tabs"
    )
    assert [tab["title"] for tab in tabs["tabs"]] == [
        "Overview",
        "Orders",
        "Materials",
        "Inventory",
        "Method",
    ]
    texts = [
        item["text"]
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "Text"
    ]
    assert any(
        "Demand 42 t · eligible finished stock 1.5 t · produce 40.5 t" in text for text in texts
    )
    tables = {
        item["title"]: item
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "AustraDataTable"
    }
    assert "Order-wise production gaps" in tables
    assert "Requirements by order" in tables
    assert "Current available raw inventory" in tables
    quartzite = next(
        row for row in tables["Raw material coverage"]["rows"] if row[0] == "Quartzite"
    )
    assert quartzite[1:4] == ["122.89725", "80", "42.89725"]
    assert any(row[0] == "SO-1" for row in tables["Requirements by order"]["rows"])


def test_material_plan_with_supplier_reasons_uses_one_complete_report(tmp_path):
    _workspace(tmp_path)
    _record(
        tmp_path,
        "supplier_candidate",
        "quartz_supplier",
        {
            "supplier_id": "VND-QZ-01",
            "supplier_name": "Quartzworks North",
            "material_category": "Quartzite",
            "city": "Jaipur",
            "state": "Rajasthan",
            "qualification_status": "verified_by_administrator",
            "notes": "Candidate not contacted, qualified, price-checked, or approved.",
        },
    )
    _record(
        tmp_path,
        "supplier_candidate",
        "powder_supplier",
        {
            "supplier_id": "VND-MG-01",
            "supplier_name": "Silicon Source",
            "material_category": "Metallurgical silicon powder",
            "notes": "Internal intermediate; backup external source. Not contacted or approved.",
        },
    )
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/chats",
        json={
            "body": "give me analysis of raw material will be needed to fulfill current orders "
            "also give supplier suggetion with reason"
        },
    )
    assert result.status_code == 201
    chat = result.json()["chat"]
    assert len(chat["messages"]) == 2
    answer = chat["messages"][-1]
    assert "122.89725" in answer["body"]
    assert "Quartzworks North" in answer["body"]
    assert "Backup external source for an internal intermediate" in answer["body"]
    assert "not a ranked or approved purchase recommendation" in answer["body"]
    surface = client.get(f"/api/ui/chats/{chat['id']}").json()["surfaces"][0]
    assert surface["visual_complete"] is True
    components = surface["messages"][-1]["updateComponents"]["components"]
    tabs = next(item for item in components if item["component"] == "Tabs")
    assert "Suppliers" in [tab["title"] for tab in tabs["tabs"]]
    table = next(
        item
        for item in components
        if item["component"] == "AustraDataTable"
        and item["title"] == "Candidate sources and reasons"
    )
    quartz = next(row for row in table["rows"] if row[2] == "VND-QZ-01")
    assert quartz[1] == "42.89725"
    assert quartz[3] == "Quartzworks North"
    assert "Listed for quartzite" in quartz[5]


def test_other_addition_uses_verified_plan_and_keeps_requested_detail(tmp_path, monkeypatch):
    _workspace(tmp_path)

    def analyze_extra(_files, _access, _chat_id, verified_material_plan=None):
        assert verified_material_plan["orders"][0]["production_kg"] == "8500"
        return AgentReply(
            reply=(
                "Pending invoices from saved evidence:\n\n"
                "| Invoice | Status |\n| --- | --- |\n| INV-1 | Pending review |"
            )
        )

    monkeypatch.setattr(web, "analyze", analyze_extra)
    client = TestClient(web.create_app(tmp_path, planner=analyze_extra))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/chats",
        json={"body": "Give raw materials needed for open orders plus pending invoices"},
    )
    assert response.status_code == 201
    answer = response.json()["chat"]["messages"][-1]
    assert "122.89725" in answer["body"]
    assert "INV-1" in answer["body"]
    surface = client.get(f"/api/ui/chats/{response.json()['chat']['id']}").json()["surfaces"][0]
    assert surface["visual_complete"] is False
    tabs = next(
        item
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "Tabs"
    )
    assert "Additional" in [tab["title"] for tab in tabs["tabs"]]


def test_visual_report_keeps_unknown_inventory_explicit(tmp_path):
    _workspace(tmp_path)
    (tmp_path / "records" / "raw_material_inventory_lot" / "raw0.json").unlink()
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    chat = client.post(
        "/api/chats",
        json={"body": "Give raw materials needed order wise and available for all orders"},
    ).json()["chat"]
    answer = chat["messages"][-1]
    assert "quartzite | 2890 | 122.89725 | unknown | unknown" in answer["body"]
    surface = client.get(f"/api/ui/chats/{chat['id']}").json()["surfaces"][0]
    material_table = next(
        item
        for item in surface["messages"][-1]["updateComponents"]["components"]
        if item["component"] == "AustraDataTable" and item["title"] == "Raw material coverage"
    )
    quartzite = next(row for row in material_table["rows"] if row[0] == "Quartzite")
    assert quartzite[1:4] == ["122.89725", "Unknown", "Unknown"]


def test_unsupported_allocation_method_does_not_present_numeric_plan(tmp_path):
    _workspace(tmp_path)
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/chats",
        json={
            "body": "Give raw materials needed order wise and available for all orders. "
            "Allocate stock equally across orders."
        },
    )
    assert result.status_code == 201
    answer = result.json()["chat"]["messages"][-1]["body"]
    assert "allocation method is not supported" in answer
    assert "122897.25" not in answer


def test_exact_decimal_tool_and_intent_boundary(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Calculate", "Admin")
    tools = ReadTools(files, chat["id"])
    result = tools.invoke(
        "calculate_decimal", {"operation": "multiply", "operands": ["40.5", "2.89", "1.05"]}
    )
    assert result["data"]["value"] == "122.89725"
    expression = tools.invoke(
        "calculate_expression",
        {
            "expression": "(production_t * ratio_t) * allowance",
            "variables": {"production_t": "40.5", "ratio_t": "2.89", "allowance": "1.05"},
        },
    )
    assert expression["data"]["value"] == "122.89725"
    with pytest.raises(ValueError, match="Only decimal arithmetic"):
        calculate_expression("__import__('os').system('echo unsafe')", {})
    with pytest.raises(ValueError, match="Missing input"):
        calculate_expression("production_t * ratio_t", {"production_t": "40.5"})
    assert is_material_plan_request("Give raw materials needed order wise and available")
    assert is_material_plan_request("Give raw meterial needed for all orders")
    assert not is_material_plan_request("Save a raw material policy for orders")
    assert is_material_plan_followup(
        "give me remaining but you didn't give me order wise",
        ["Give raw materials needed order wise and available for all orders"],
    )
    assert not is_material_plan_followup(
        "give supplier suggestions",
        ["Give raw materials needed order wise and available for all orders"],
    )
    assert material_plan_additions("raw materials for orders and vendor recommendations") == {
        "suppliers"
    }
    assert material_plan_additions("raw materials for orders plus pending invoices") == {"other"}
    assert not unsupported_allocation_request("Show stock allocation for each order")
    assert not unsupported_allocation_request("Allocate stock by earliest requested delivery")
    assert unsupported_allocation_request("Allocate stock equally across orders")
