import json

import pytest

from austra.files import Files


def test_fresh_workspace_has_no_business_schema_or_database(tmp_path):
    files = Files(tmp_path / "blank")

    assert files.chats() == []
    assert files.drafts() == []
    assert files.approved() == []
    assert list(files.root.glob("*.db")) == []
    assert not (files.root / "approved").exists()


def test_chat_drafts_publish_into_versioned_folders(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Define the first stock record", "Admin")
    files.add_message(chat["id"], "assistant", "I will draft a schema.", "Austra")
    schema = json.dumps({"type": "object", "properties": {"quantity": {"type": "number"}}})
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "schema",
                "name": "stock_lot",
                "summary": "Proposed stock lot fields",
                "content": schema,
            }
        ],
    )[0]

    assert files.approved() == []
    files.review(draft["id"], "approved")
    assert (tmp_path / "approved/schema/stock_lot/v1.json").exists()
    assert files.approved()[0]["content"].strip() == schema


def test_drafts_reject_path_traversal_and_credentials_atomically(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Make a skill", "Admin")
    good = {"kind": "skill", "name": "planner", "summary": "Planning", "content": "# Planning"}
    bad = {"kind": "skill", "name": "../escape", "summary": "Bad", "content": "text"}

    with pytest.raises(ValueError, match="Invalid artifact"):
        files.add_drafts(chat["id"], [good, bad])
    assert files.drafts() == []

    with pytest.raises(ValueError, match="credential"):
        files.add_drafts(
            chat["id"],
            [{"kind": "policy", "name": "bad_secret", "summary": "Bad", "content": "sk-ant-test"}],
        )
    assert files.drafts() == []


def test_record_requires_user_created_schema_and_is_validated(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Track quantity", "Admin")
    record = {
        "kind": "record",
        "name": "quantity_entry",
        "summary": "A new quantity",
        "content": '{"quantity": 12}',
    }
    with pytest.raises(ValueError, match="approved schema"):
        files.add_drafts(chat["id"], [record])

    schema = {
        "type": "object",
        "properties": {"quantity": {"type": "number"}},
        "required": ["quantity"],
        "additionalProperties": False,
    }
    definition = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "schema",
                "name": "quantity_entry",
                "summary": "Quantity schema",
                "content": json.dumps(schema),
            }
        ],
    )[0]
    files.review(definition["id"], "approved")
    with pytest.raises(ValueError, match="does not match"):
        files.add_drafts(
            chat["id"],
            [{**record, "content": '{"quantity": "unknown"}'}],
        )

    proposal = files.add_drafts(chat["id"], [record])[0]
    files.review(proposal["id"], "approved")
    assert files.records()[0]["values"] == {"quantity": 12}
    assert files.records()[0]["schema_version"] == 1


def test_repeating_an_import_does_not_duplicate_identical_records(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Add supplier list", "Admin")
    drafts = [
        {
            "kind": "schema",
            "name": "supplier_candidate",
            "summary": "Supplier candidates",
            "content": json.dumps(
                {
                    "type": "object",
                    "properties": {"supplier_id": {"type": "string"}},
                    "required": ["supplier_id"],
                }
            ),
        },
        {
            "kind": "record",
            "name": "supplier_candidate",
            "summary": "Supplier candidate",
            "content": '{"supplier_id": "DEMO-01"}',
        },
    ]

    assert len(files.apply_agent_drafts(chat["id"], drafts, ["foundation"])) == 2
    assert files.apply_agent_drafts(chat["id"], drafts, ["foundation"]) == []
    assert len(files.records()) == 1


def test_admin_can_update_and_soft_delete_records_with_audit_history(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Correct and remove a stock record", "Admin")
    schema = {
        "type": "object",
        "properties": {"quantity": {"type": "number"}},
        "required": ["quantity"],
        "additionalProperties": False,
    }
    files.apply_agent_drafts(
        chat["id"],
        [
            {
                "kind": "schema",
                "name": "stock_lot",
                "summary": "Stock lot",
                "content": json.dumps(schema),
            },
            {
                "kind": "record",
                "name": "stock_lot",
                "summary": "One stock lot",
                "content": '{"quantity": 12}',
            },
        ],
        ["foundation"],
    )
    record_id = files.records()[0]["id"]

    updated = files.update_record(chat["id"], record_id, {"quantity": 9}, "Count corrected")
    assert updated["values"] == {"quantity": 9}
    history = list((tmp_path / "record_history/stock_lot" / record_id).glob("*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text(encoding="utf-8"))["values"] == {"quantity": 12}

    assert files.delete_records(chat["id"], "all", "", "Added by mistake") == 1
    assert files.delete_records(chat["id"], "all", "", "Added by mistake") == 0
    assert files.records() == []
    assert files.records(include_deleted=True)[0]["values"] == {"quantity": 9}
    assert files.records(include_deleted=True)[0]["deleted_at"]
    actions = {entry["action"] for entry in files.audit_entries()}
    assert {"record.updated", "record.deleted"} <= actions
