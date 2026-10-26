import hashlib
import io
import json

from fastapi.testclient import TestClient
from openpyxl import Workbook

from austra.agent import AgentReply, Draft
from austra.files import Files
from austra.imports import import_chat_tables
from austra.web import create_app

SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "lot_id": {"type": "string"},
            "on_hand_kg": {"type": "number"},
            "data_origin": {"type": "string"},
            "provenance": {
                "type": "object",
                "properties": {
                    "source_file": {"type": "string"},
                    "source_hash": {"type": "string"},
                },
                "required": ["source_file", "source_hash"],
            },
        },
        "required": ["lot_id", "on_hand_kg", "data_origin", "provenance"],
    }
)


def stock_planner(_files, _access, _chat):
    return AgentReply(
        reply="Processing the stock file.",
        save_records=True,
        drafts=[Draft(kind="schema", name="stock_lot", summary="Stock lots", content=SCHEMA)],
    )


def test_csv_stock_is_saved_without_schema_approval_and_is_idempotent(tmp_path):
    client = TestClient(create_app(tmp_path, planner=stock_planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"lot_id,on_hand_kg,data_origin\nLOT-1,5,synthetic_demo\nLOT-2,7,synthetic_demo\n"
    first = client.post(
        "/api/messages", files={"uploads": ("stock.csv", content, "text/csv")}
    ).json()

    assert "Saved 2 business record(s)" in first["chat"]["messages"][-1]["body"]
    assert first["drafts"][0]["status"] == "approved"
    assert len(client.get("/api/records").json()) == 2
    assert client.get("/api/records").json()[0]["values"]["data_origin"] == "synthetic_demo"
    assert (
        client.get("/api/records").json()[0]["values"]["provenance"]["source_file"] == "stock.csv"
    )

    client.post(f"/api/chats/{first['chat']['id']}/messages", json={"body": "Add the stock again"})
    assert len(client.get("/api/records").json()) == 2
    assert len(client.get("/api/artifacts").json()) == 1


def test_invalid_csv_row_saves_no_records(tmp_path):
    client = TestClient(create_app(tmp_path, planner=stock_planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"lot_id,on_hand_kg,data_origin\nLOT-1,5,synthetic_demo\nLOT-2,wrong,synthetic_demo\n"
    result = client.post(
        "/api/messages", files={"uploads": ("stock.csv", content, "text/csv")}
    ).json()

    assert "invalid on_hand_kg" in result["chat"]["messages"][-1]["body"]
    assert client.get("/api/records").json() == []


def test_out_of_scope_request_cannot_write_drafts_or_records(tmp_path):
    def deny(_files, _access, _chat):
        return AgentReply(
            reply="This is outside your business workspace.",
            decision="deny",
            save_records=True,
            drafts=[Draft(kind="schema", name="stock_lot", summary="Stock", content=SCHEMA)],
        )

    client = TestClient(create_app(tmp_path, planner=deny))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/messages",
        data={"body": "Write an unrelated poem"},
        files={"uploads": ("stock.csv", b"lot_id,on_hand_kg,data_origin\nL,1,demo", "text/csv")},
    ).json()

    assert result["drafts"] == []
    assert client.get("/api/records").json() == []
    assert client.get("/api/artifacts").json() == []
    assert any(item["action"] == "agent.request_denied" for item in client.get("/api/audit").json())


def test_xlsx_stock_rows_are_imported(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["lot_id", "on_hand_kg", "data_origin"])
    sheet.append(["LOT-3", 9, "synthetic_demo"])
    data = io.BytesIO()
    workbook.save(data)

    client = TestClient(create_app(tmp_path, planner=stock_planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/messages",
        files={
            "uploads": (
                "stock.xlsx",
                data.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    ).json()

    assert "Saved 1 business record(s)" in result["chat"]["messages"][-1]["body"]
    assert client.get("/api/records").json()[0]["values"]["on_hand_kg"] == 9


def test_legacy_csv_attachment_is_restored_to_history_and_imported(tmp_path):
    files = Files(tmp_path)
    content = b"lot_id,on_hand_kg,data_origin\nLOT-4,4,synthetic_demo\n"
    digest = hashlib.sha256(content).hexdigest()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / f"{digest}.csv").write_bytes(content)
    chat = files.create_chat(f"Attachment stock.csv ({digest}):\n{content.decode()}", "Admin")
    schema = files.add_drafts(
        chat["id"],
        [{"kind": "schema", "name": "stock_lot", "summary": "Stock", "content": SCHEMA}],
    )[0]
    files.review(schema["id"], "approved")

    assert files.chat(chat["id"])["messages"][0]["attachments"][0]["name"] == "stock.csv"
    assert import_chat_tables(files, chat["id"]) == (1, [])
    assert files.records()[0]["values"]["lot_id"] == "LOT-4"


def test_large_tables_create_distinct_schemas_and_import_without_model(tmp_path):
    def unavailable_planner(_files, _access, _chat):
        raise AssertionError("Large structured tables should not be copied through the model")

    client = TestClient(create_app(tmp_path, planner=unavailable_planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    notes = "x" * 31000
    purchase = f"purchase_order_id,ordered_kg,notes\nPO-1,10,{notes}\nPO-2,12,ok\n".encode()
    sales = b"sales_order_id,shipped_kg\nSO-1,7\n"
    first = client.post(
        "/api/messages",
        data={"body": "Add these data"},
        files=[
            ("uploads", ("historical_purchase_orders.csv", purchase, "text/csv")),
            ("uploads", ("historical_sales_orders.csv", sales, "text/csv")),
        ],
    )
    assert first.status_code == 200
    assert "Saved 3 business record(s)" in first.json()["chat"]["messages"][-1]["body"]
    records = client.get("/api/records").json()
    assert {record["schema"] for record in records} == {
        "historical_purchase_order",
        "historical_sales_order",
    }
    assert (
        next(record for record in records if record["values"].get("purchase_order_id") == "PO-1")[
            "values"
        ]["notes"]
        == notes
    )
    assert (
        next(record for record in records if record["values"].get("sales_order_id") == "SO-1")[
            "values"
        ]["shipped_kg"]
        == 7
    )
    chat_id = first.json()["chat"]["id"]
    again = client.post(f"/api/chats/{chat_id}/messages", json={"body": "continue"})
    assert "No new rows needed saving" in again.json()["chat"]["messages"][-1]["body"]
    assert len(client.get("/api/records").json()) == 3

    corrected = purchase.replace(notes.encode(), f"{notes} corrected".encode())
    update = client.post(
        "/api/messages",
        data={"chat_id": chat_id, "body": "Add corrected data"},
        files={"uploads": ("historical_purchase_orders.csv", corrected, "text/csv")},
    )
    assert "Saved 1 business record(s)" in update.json()["chat"]["messages"][-1]["body"]
    assert len(client.get("/api/records").json()) == 3
    assert (
        len(list((tmp_path / "record_history" / "historical_purchase_order").glob("*/*.json"))) == 1
    )


def test_revised_table_extends_schema_and_keeps_unsupplied_values(tmp_path):
    files = Files(tmp_path)

    def attach(chat_id, content):
        digest = hashlib.sha256(content).hexdigest()
        chat, _, _ = files.submit_message(
            chat_id,
            "Add sales data",
            [
                {
                    "id": digest,
                    "name": "historical_sales_orders.csv",
                    "extension": ".csv",
                    "content": content,
                    "text": content.decode(),
                    "excerpt": content.decode(),
                }
            ],
        )
        return chat["id"]

    chat_id = attach(None, b"sales_order_id,shipped_kg,notes\nSO-1,7,original\n")
    assert import_chat_tables(files, chat_id, auto_schema=True) == (1, [])
    attach(chat_id, b"sales_order_id,shipped_kg,new_field\nSO-1,8,extra\n")
    assert import_chat_tables(files, chat_id, auto_schema=True) == (1, [])
    assert len(files.records()) == 1
    assert files.records()[0]["values"]["notes"] == "original"
    assert files.records()[0]["values"]["new_field"] == "extra"
    schema = next(item for item in files.approved() if item["name"] == "historical_sales_order")
    assert "new_field" in json.loads(schema["content"])["properties"]
