import json

from fastapi.testclient import TestClient

from austra.agent import AgentReply, RuleChange
from austra.files import Files
from austra.web import create_app


def _stock_record(tmp_path):
    folder = tmp_path / "records" / "raw_material_inventory_lot"
    folder.mkdir(parents=True)
    record = {
        "id": "a" * 32,
        "schema": "raw_material_inventory_lot",
        "approved_at": "2026-10-23T00:00:00Z",
        "values": {
            "lot_id": "RM-1",
            "material_name": "Quartz",
            "verification_status": "unverified",
            "on_hand_kg": 171500,
            "reserved_kg": 1500,
            "qc_hold_kg": 0,
            "available_kg": 170000,
        },
    }
    (folder / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")


def test_admin_unit_instruction_activates_markdown_rule_and_converts_display_only(tmp_path):
    _stock_record(tmp_path)

    def no_model(_files, _access, _chat_id):
        raise AssertionError("Direct administrator unit instructions should not need the model")

    client = TestClient(create_app(tmp_path, planner=no_model))
    client.post("/api/setup", json={"password": "long private passphrase"})
    changed = client.post(
        "/api/chats",
        json={"body": "From now on show stock in metric tons; keep underlying values in kg"},
    )
    assert "Saved values remain in kilograms" in changed.json()["chat"]["messages"][-1]["body"]
    rules = client.get("/api/rules").json()
    assert len(rules) == 1
    assert rules[0]["name"] == "stock_display_units"
    assert (tmp_path / "approved/policy/stock_display_units/v1.md").exists()

    shown = client.post("/api/chats", json={"body": "show me current stock"})
    reply = shown.json()["chat"]["messages"][-1]["body"]
    assert "On hand (t)" in reply
    assert "171.5" in reply
    assert "1.5" in reply
    assert client.get("/api/records").json()[0]["values"]["on_hand_kg"] == 171500
    assert client.get("/api/records").json()[0]["values"]["verification_status"] == "unverified"

    listing = client.post("/api/chats", json={"body": "Show me rules for stock display"})
    assert "stock_display_units" in listing.json()["chat"]["messages"][-1]["body"]
    named = client.post("/api/chats", json={"body": "What is the stock_display_units rule?"})
    assert "Display unit: metric tons (t)" in named.json()["chat"]["messages"][-1]["body"]
    no_update_rule = client.post("/api/chats", json={"body": "Show me rules for inventory update"})
    assert (
        "No active administrator-approved rules"
        in no_update_rule.json()["chat"]["messages"][-1]["body"]
    )


def test_explicit_rule_change_activates_and_retires_separate_policy(tmp_path):
    def planner(files, _access, chat_id):
        message = files.chat(chat_id)["messages"][-1]["body"]
        if "remove" in message.casefold():
            change = RuleChange(
                action="retire", name="inventory_updates", reason="Admin removed it"
            )
        else:
            change = RuleChange(
                action="activate",
                name="inventory_updates",
                content="# Inventory updates\n\nRequire a manager sign-off for inventory updates.",
                reason="Admin set the rule",
            )
        return AgentReply(reply="Done.", rule_changes=[change])

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    added = client.post("/api/chats", json={"body": "Set an inventory update rule"})
    assert "Activated 1 business rule" in added.json()["chat"]["messages"][-1]["body"]
    assert len(client.get("/api/rules").json()) == 1
    removed = client.post("/api/chats", json={"body": "Remove that inventory update rule"})
    assert "Retired 1 business rule" in removed.json()["chat"]["messages"][-1]["body"]
    assert client.get("/api/rules").json() == []
    assert (tmp_path / "approved/policy/inventory_updates/v1.md").exists()


def test_suggested_rule_remains_a_draft_even_if_planner_requests_activation(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="I activated a suggested rule.",
            rule_changes=[
                RuleChange(
                    action="activate",
                    name="inventory_updates",
                    content="# Inventory updates\n\nRequire manager sign-off.",
                    reason="Suggestion for review",
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post("/api/chats", json={"body": "Suggest a rule for inventory updates"})
    assert "not active" in response.json()["chat"]["messages"][-1]["body"]
    assert response.json()["drafts"][0]["status"] == "proposed"
    assert client.get("/api/rules").json() == []


def test_clear_admin_verification_command_updates_contextual_schema_without_evidence_gate(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Is raw material verified?", "Admin")
    folder = tmp_path / "records" / "raw_material_inventory_lot"
    folder.mkdir(parents=True)
    schema = {
        "type": "object",
        "properties": {"verification_status": {"type": "string"}, "lot_id": {"type": "string"}},
        "required": ["verification_status", "lot_id"],
    }
    files.apply_agent_drafts(
        chat["id"],
        [
            {
                "kind": "schema",
                "name": "raw_material_inventory_lot",
                "summary": "Raw lots",
                "content": json.dumps(schema),
            }
        ],
        ["foundation"],
    )
    files.save_imported_records(
        chat["id"],
        "raw_material_inventory_lot",
        "b" * 64,
        "raw.csv",
        [(2, {"lot_id": "RM-1", "verification_status": "unverified"})],
    )

    def no_model(_files, _access, _chat_id):
        raise AssertionError("Clear administrator status updates should not need the model")

    client = TestClient(create_app(tmp_path, planner=no_model))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post(
        "/api/messages",
        data={"chat_id": chat["id"], "body": "Update all records to verified; I got a QA report"},
    )
    assert "Updated 1 business record" in result.json()["chat"]["messages"][-1]["body"]
    assert files.records()[0]["values"]["verification_status"] == "verified"
    assert len(list((tmp_path / "record_history").glob("*/*/*.json"))) == 1


def test_active_admin_policy_prevents_direct_verification_shortcut(tmp_path):
    _stock_record(tmp_path)
    files = Files(tmp_path)
    chat = files.create_chat("Is raw material verified?", "Admin")
    files.save_policy(
        chat["id"],
        "inventory_verification",
        "# Inventory verification\n\nRequire a report.",
        "Admin rule",
    )
    called = []

    def planner(_files, _access, _chat_id):
        called.append(True)
        return AgentReply(reply="The active rule requires a report.")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    client.post(
        "/api/messages",
        data={"chat_id": chat["id"], "body": "Update all records to verified"},
    )
    assert called
    assert files.records()[0]["values"]["verification_status"] == "unverified"
