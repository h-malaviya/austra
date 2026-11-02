import hashlib
import json

import pytest

from austra.files import Files
from austra.tools import ReadTools


def test_provider_tool_definitions_keep_input_limits_without_strict_mode(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Check tool schema", "Admin")
    tools = ReadTools(files, chat["id"])
    definitions = {item["name"]: item for item in tools.definitions()}

    assert all("strict" not in item for item in definitions.values())
    assert (
        definitions["compose_record_lookup"]["input_schema"]["properties"]["schemas"]["maxItems"]
        == 8
    )
    with pytest.raises(ValueError):
        tools.invoke("compose_record_lookup", {"schemas": ["stock"] * 9})


def test_read_tools_search_approved_files_and_audit_use(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("What is our quartz quality rule?", "Admin")
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "policy",
                "name": "quartz_quality",
                "summary": "Quality guidance",
                "content": "Only inspect the quartz grade after approval.",
            }
        ],
    )[0]
    files.review(draft["id"], "approved")

    tools = ReadTools(files, chat["id"])
    found = tools.invoke("search_workspace", {"query": "quartz"})
    assert any(item["name"] == "quartz_quality" for item in found["data"])
    read = tools.invoke("read_artifact", {"kind": "policy", "name": "quartz_quality"})
    assert "inspect" in read["data"]["content"]
    events = files.audit_entries()
    assert len([item for item in events if item["action"] == "agent.tool_used"]) == 2
    assert any(item["action"] == "draft.approved" for item in events)


def test_read_attachment_uses_saved_text_and_chat_access(tmp_path):
    files = Files(tmp_path)
    content = b"supplier notes with stock facts"
    digest = hashlib.sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Read the supplier document",
        [
            {
                "id": digest,
                "name": "supplier.txt",
                "extension": ".txt",
                "content": content,
                "text": content.decode(),
                "excerpt": content.decode(),
            }
        ],
    )
    result = ReadTools(files, chat["id"]).invoke(
        "read_attachment", {"attachment_id": digest, "offset": 9}
    )
    assert result["data"]["text"] == "notes with stock facts"


def test_list_records_preserves_supplier_fields_and_paginates(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Show suppliers", "Admin")
    folder = tmp_path / "records" / "supplier_candidate"
    folder.mkdir(parents=True)
    for index in range(3):
        record = {
            "id": f"{index:032x}",
            "schema": "supplier_candidate",
            "approved_at": f"2026-10-23T00:00:0{index}+00:00",
            "values": {
                "supplier_id": f"SUP-{index}",
                "supplier_name": f"Supplier {index}",
                "material_category": "quartzite",
                "contact_email": f"supplier{index}@example.com",
            },
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")

    tools = ReadTools(files, chat["id"])
    first = tools.invoke("list_records", {"schema": "supplier_candidate", "limit": 2})["data"]
    second = tools.invoke(
        "list_records", {"schema": "supplier_candidate", "offset": first["next_offset"]}
    )["data"]

    assert first["total"] == 3
    assert first["next_offset"] == 2
    assert first["records"][0]["values"]["supplier_name"] == "Supplier 0"
    assert first["records"][0]["values"]["contact_email"] == "supplier0@example.com"
    assert second["records"][0]["values"]["supplier_id"] == "SUP-2"
    assert second["next_offset"] is None


def test_research_lookup_collects_every_page_without_repeating_records(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Compare stock with open orders", "Admin")
    folder = tmp_path / "records" / "finished_goods_stock_lot"
    folder.mkdir(parents=True)
    for index in range(11):
        record = {
            "id": f"{index:032x}",
            "schema": "finished_goods_stock_lot",
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {
                "lot_id": f"LOT-{index:02d}",
                "available_kg": 100 + index,
                "notes": "quality details " * 30,
            },
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")

    result = ReadTools(files, chat["id"]).invoke_for_research(
        "list_records", {"schema": "finished_goods_stock_lot"}
    )["data"]

    assert result["complete"] is True
    assert result["total"] == 11
    assert result["next_offset"] is None
    assert [record["values"]["lot_id"] for record in result["records"]] == [
        f"LOT-{index:02d}" for index in range(11)
    ]


def test_stalled_record_page_recovers_from_snapshot_and_remembers_remedy(tmp_path, monkeypatch):
    files = Files(tmp_path)
    chat = files.create_chat("Read all stock", "Admin")
    folder = tmp_path / "records" / "stock_lot"
    folder.mkdir(parents=True)
    for index in range(3):
        record = {
            "id": f"{index:032x}",
            "schema": "stock_lot",
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {"lot_id": f"LOT-{index}"},
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
    tools = ReadTools(files, chat["id"])
    first = tools.invoke("list_records", {"schema": "stock_lot", "limit": 2})["data"]

    def stalled(_name, _raw_input):
        return {"truncated": False, "data": first}

    monkeypatch.setattr(tools, "invoke", stalled)
    result = tools.invoke_for_research("list_records", {"schema": "stock_lot", "limit": 2})["data"]

    assert result["complete"] is True
    assert result["recovered_from"] == "pagination_stalled"
    assert [record["values"]["lot_id"] for record in result["records"]] == [
        "LOT-0",
        "LOT-1",
        "LOT-2",
    ]
    assert files.recovery_lessons() == [
        {
            "tool": "list_records",
            "failure": "pagination_stalled",
            "resolution": "snapshot_record_lookup",
        }
    ]
    history = ReadTools(files, chat["id"]).invoke_for_research("list_recovery_history", {})["data"]
    assert history[0]["failure"] == "pagination_stalled"
    assert history[0]["resolution"] == "snapshot_record_lookup"
    assert "LOT-0" not in json.dumps(history)

    incomplete_first = {**first, "next_offset": None, "complete": False}
    monkeypatch.setattr(
        tools,
        "invoke",
        lambda _name, _raw_input: {"truncated": False, "data": incomplete_first},
    )
    recovered = tools.invoke_for_research("list_records", {"schema": "stock_lot"})["data"]
    assert recovered["complete"] is True
    assert [record["values"]["lot_id"] for record in recovered["records"]] == [
        "LOT-0",
        "LOT-1",
        "LOT-2",
    ]


def test_composed_lookup_reads_multiple_schemas_in_one_complete_result(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Compare stock and orders", "Admin")
    for index, schema in enumerate(("stock_lot", "sales_order")):
        folder = tmp_path / "records" / schema
        folder.mkdir(parents=True)
        record = {
            "id": f"{index:032x}",
            "schema": schema,
            "approved_at": f"2026-10-23T00:00:{index:02d}+00:00",
            "values": {"reference": f"REF-{index}"},
        }
        (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")

    result = ReadTools(files, chat["id"]).invoke_for_research(
        "compose_record_lookup",
        {"schemas": ["stock_lot", "sales_order"], "save_as": "stock_order_match"},
    )["data"]

    assert result["complete"] is True
    assert result["saved_as"] == "stock_order_match"
    assert result["schemas"]["stock_lot"]["records"][0]["values"]["reference"] == "REF-0"
    assert result["schemas"]["sales_order"]["records"][0]["values"]["reference"] == "REF-1"
    assert files.read_plans()[0]["name"] == "stock_order_match"
    replay = ReadTools(Files(tmp_path), chat["id"]).invoke_for_research(
        "run_saved_record_lookup", {"name": "stock_order_match"}
    )["data"]
    assert replay["schemas"] == result["schemas"]


def test_inspect_table_reads_all_rows_without_returning_row_contents(tmp_path):
    files = Files(tmp_path)
    content = b"purchase_order_id,ordered_kg,notes\nPO-1,10,first\nPO-2,,second\n"
    digest = hashlib.sha256(content).hexdigest()
    chat, _, _ = files.submit_message(
        None,
        "Inspect this table",
        [
            {
                "id": digest,
                "name": "historical_purchase_orders.csv",
                "extension": ".csv",
                "content": content,
                "text": content.decode(),
                "excerpt": "",
            }
        ],
    )
    result = ReadTools(files, chat["id"]).invoke(
        "inspect_attachment_table", {"attachment_id": digest}
    )
    section = result["data"]["sections"][0]
    assert section["row_count"] == 2
    assert section["candidate_schema"] == "historical_purchase_order"
    assert section["columns"]["ordered_kg"] == {"type": ["integer", "null"], "blank_rows": 1}
    assert "first" not in json.dumps(result)
