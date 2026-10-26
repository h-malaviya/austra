import json

from fastapi.testclient import TestClient

from austra.files import Files
from austra.reports import is_stock_request, stock_report
from austra.tools import ReadTools
from austra.web import create_app


def stock_records():
    return [
        {
            "id": f"record-{number}",
            "schema": "stock_lot",
            "approved_at": f"2026-10-23T00:{number:02d}:00Z",
            "values": {
                "lot_id": f"LOT-{number:02d}",
                "on_hand_kg": 10,
                "reserved_kg": 2,
                "available_kg": 8,
                "data_origin": "synthetic_demo",
                "notes": "saved | counted" if number == 1 else "saved",
            },
        }
        for number in range(1, 12)
    ]


def test_stock_report_includes_every_lot_and_correct_totals():
    report = stock_report(stock_records())

    assert "Stock on record (11 records)" in report
    assert all(f"LOT-{number:02d}" in report for number in range(1, 12))
    assert "| Lot ID | On hand (kg) | Reserved (kg) | Available (kg) | Notes |" in report
    assert "| --- | --- | --- | --- | --- |" in report
    assert "saved \\| counted" in report
    assert "On hand (kg): **110**" in report
    assert "Reserved (kg): **22**" in report
    assert "Available (kg): **88**" in report
    assert "demo records" in report


def test_stock_request_routes_only_unfiltered_stock_lists():
    assert is_stock_request("show me all stock")
    assert is_stock_request("shoe me current tock")
    assert not is_stock_request("show me stock for supplier A")
    assert not is_stock_request("add these in stock")


def test_stock_query_uses_saved_records_without_model_omissions(tmp_path):
    folder = tmp_path / "records" / "stock_lot"
    folder.mkdir(parents=True)
    for record in stock_records():
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")

    def fail_planner(_files, _access, _chat_id):
        raise AssertionError("A complete stock list should come from saved records")

    client = TestClient(create_app(tmp_path, planner=fail_planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post("/api/chats", json={"body": "show me all stock"})

    assert result.status_code == 201
    reply = result.json()["chat"]["messages"][-1]["body"]
    assert all(f"LOT-{number:02d}" in reply for number in range(1, 12))
    assert "On hand (kg): **110**" in reply
    assert "3 more" not in reply

    files = Files(tmp_path)
    listed = ReadTools(files, result.json()["chat"]["id"]).invoke(
        "list_records", {"offset": 8, "limit": 3}
    )
    assert listed["truncated"] is False
    assert listed["data"]["total"] == 11
    assert listed["data"]["next_offset"] is None
    assert len(listed["data"]["records"]) == 3
