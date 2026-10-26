import hashlib
import json

from austra.files import Files
from austra.tools import ReadTools


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
