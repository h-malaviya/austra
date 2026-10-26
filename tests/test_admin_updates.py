import json

import pytest
from fastapi.testclient import TestClient

from austra.agent import AgentReply, RecordChange
from austra.files import Files
from austra.skills import load_skills
from austra.web import create_app

SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "lot_id": {"type": "string"},
            "verification_status": {"type": "string"},
            "on_hand_kg": {"type": "number"},
        },
        "required": ["lot_id", "verification_status", "on_hand_kg"],
    }
)
SOURCE = "a" * 64
OTHER_SOURCE = "b" * 64


def seeded_files(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Mark this import verified", "Admin")
    files.apply_agent_drafts(
        chat["id"],
        [{"kind": "schema", "name": "stock_lot", "summary": "Stock", "content": SCHEMA}],
        ["foundation"],
    )
    for source, lot in ((SOURCE, "A"), (SOURCE, "B"), (OTHER_SOURCE, "C")):
        files.save_imported_records(
            chat["id"],
            "stock_lot",
            source,
            "stock.csv",
            [(ord(lot), {"lot_id": lot, "verification_status": "unverified", "on_hand_kg": 5})],
        )
    return files, chat["id"]


def test_admin_bulk_update_is_scoped_and_keeps_original_versions(tmp_path):
    files, chat_id = seeded_files(tmp_path)
    changed = files.update_records(
        chat_id, "stock_lot", {"verification_status": "verified"}, "Admin instruction", SOURCE
    )

    assert changed == 2
    records = {record["values"]["lot_id"]: record for record in files.records()}
    assert [records[lot]["values"]["verification_status"] for lot in ("A", "B", "C")] == [
        "verified",
        "verified",
        "unverified",
    ]
    assert all(records[lot]["source_attachment"] == SOURCE for lot in ("A", "B"))
    history = list((tmp_path / "record_history" / "stock_lot").glob("*/*.json"))
    assert len(history) == 2
    assert all(
        json.loads(path.read_text())["values"]["verification_status"] == "unverified"
        for path in history
    )
    assert (
        len([event for event in files.audit_entries() if event["action"] == "record.updated"]) == 2
    )


def test_bulk_update_validates_every_result_before_writing(tmp_path):
    files, chat_id = seeded_files(tmp_path)
    with pytest.raises(ValueError, match="schema"):
        files.update_records(chat_id, "stock_lot", {"on_hand_kg": "bad"}, "Wrong type", SOURCE)
    assert all(record["values"]["on_hand_kg"] == 5 for record in files.records())
    assert not list((tmp_path / "record_history").glob("*/*/*.json"))


def test_chat_bulk_update_applies_and_stock_report_shows_verification(tmp_path):
    seeded_files(tmp_path)

    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="I marked the imported lots verified on your instruction.",
            record_changes=[
                RecordChange(
                    action="update",
                    scope="schema",
                    schema_name="stock_lot",
                    source_attachment=SOURCE,
                    values_json='{"verification_status":"verified"}',
                    reason="Administrator instructed this change",
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post("/api/chats", json={"body": "Mark the imported lots verified"})
    assert "Updated 2 business record(s)" in response.json()["chat"]["messages"][-1]["body"]
    report = client.post("/api/chats", json={"body": "Show stock"})
    assert "Verification" in report.json()["chat"]["messages"][-1]["body"]
    assert "verified" in report.json()["chat"]["messages"][-1]["body"]


def test_approved_policies_are_loaded_with_platform_skills(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Create rule", "Admin")
    draft = files.add_drafts(
        chat["id"],
        [
            {
                "kind": "policy",
                "name": "verification_gate",
                "summary": "Gate",
                "content": "Require a manager sign-off before verification.",
            }
        ],
    )[0]
    files.review(draft["id"], "approved")
    assert "Require a manager sign-off before verification." in load_skills(files)
