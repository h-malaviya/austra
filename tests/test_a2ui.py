from fastapi.testclient import TestClient

from austra.a2ui import CATALOG_ID, REPORT_CATALOG_ID, VERSION, Surface, _tonnes
from austra.a2ui_tables import markdown_tables, table_root, visual_is_complete
from austra.agent import AgentReply, Draft
from austra.web import create_app


def _components(surface):
    return surface["messages"][-1]["updateComponents"]["components"]


def test_draft_review_surface_uses_a2ui_and_existing_review_endpoint(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="I drafted a rule for your review.",
            drafts=[
                Draft(
                    kind="policy",
                    name="approval_rule",
                    summary="Manager approval is needed.",
                    content="# Approval rule\nAsk the manager first.",
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    assert client.get("/api/ui/workspace").status_code == 401
    client.post("/api/setup", json={"password": "long private passphrase"})
    created = client.post("/api/chats", json={"body": "Draft an approval rule"}).json()
    chat_id = created["chat"]["id"]
    draft_id = created["drafts"][0]["id"]

    surface = client.get(f"/api/ui/chats/{chat_id}").json()["surfaces"][0]
    assert surface["messages"][0] == {
        "version": VERSION,
        "createSurface": {"surfaceId": f"draft_{draft_id}", "catalogId": CATALOG_ID},
    }
    assert surface["source"] == "# Approval rule\nAsk the manager first."
    assert any(component["id"] == "root" for component in _components(surface))
    actions = [
        component["action"]["event"] for component in _components(surface) if "action" in component
    ]
    assert {action["name"] for action in actions} == {"approve_draft", "reject_draft"}
    assert all(action["context"]["draft_id"] == draft_id for action in actions)

    approved = client.post(f"/api/drafts/{draft_id}/review", json={"decision": "approved"})
    assert approved.status_code == 200
    refreshed = client.get(f"/api/ui/chats/{chat_id}").json()["surfaces"][0]
    assert not any("action" in component for component in _components(refreshed))


def test_answer_surface_only_appears_for_current_question(tmp_path):
    def planner(files, _access, chat_id):
        if files.chat(chat_id)["messages"][-1]["body"] == "Blue":
            return AgentReply(reply="Thanks, I have the color.")
        return AgentReply(reply="Which color should I use?", needs_input=True)

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    created = client.post("/api/chats", json={"body": "Set up the product"}).json()
    chat_id = created["chat"]["id"]
    assert created["chat"]["messages"][-1]["needs_input"] is True

    surface = client.get(f"/api/ui/chats/{chat_id}").json()["surfaces"][0]
    assert surface["kind"] == "answer"
    assert surface["messages"][1]["updateDataModel"]["value"] == ""
    button = next(
        component for component in _components(surface) if component["component"] == "Button"
    )
    assert button["action"]["event"] == {
        "name": "submit_answer",
        "context": {"answer": {"path": "/answer"}},
    }

    client.post(f"/api/chats/{chat_id}/messages", json={"body": "Blue"})
    assert client.get(f"/api/ui/chats/{chat_id}").json()["surfaces"] == []


def test_workspace_surfaces_show_saved_records_and_pending_reviews(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="Saved a stock item and proposed a rule.",
            drafts=[
                Draft(
                    kind="record", name="stock", summary="Stock item", content='{"quantity": 12}'
                ),
                Draft(kind="policy", name="stock_rule", summary="Stock rule", content="# Rule"),
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    created = client.post("/api/chats", json={"body": "Save stock and draft a rule"}).json()
    chat_surfaces = client.get(f"/api/ui/chats/{created['chat']['id']}").json()["surfaces"]
    assert {surface["kind"] for surface in chat_surfaces} == {"draft", "records"}

    records = client.get("/api/ui/workspace?section=records").json()
    assert records["total"] == 1
    assert records["page_size"] == 20
    assert records["items"][0]["source"] == '{\n  "quantity": 12\n}'
    assert any(
        "quantity: 12" in component.get("text", "") for component in _components(records["surface"])
    )
    drafts = client.get("/api/ui/workspace?section=drafts").json()
    assert drafts["total"] == 1
    assert drafts["items"][0]["title"] == "policy · stock_rule"
    assert client.get("/api/ui/workspace?section=unknown").status_code == 400
    assert client.get("/api/ui/workspace?offset=-1").status_code == 400


def test_tabular_answer_is_one_visual_message_with_text_fallback(tmp_path):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply=(
                "Current balance is 8 kg.\n\n"
                "| Material | Needed (kg) | Available (kg) |\n"
                "| --- | ---: | ---: |\n"
                "| Quartzite | 10 | 8 |"
            )
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    created = client.post("/api/chats", json={"body": "Show the balance"}).json()
    answer = created["chat"]["messages"][-1]
    assert answer["visualize"] is True
    assert answer["display_body"] == "Current balance is 8 kg."
    assert "Quartzite" in answer["body"]
    surface = client.get(f"/api/ui/chats/{created['chat']['id']}").json()["surfaces"][0]
    assert surface["kind"] == "report"
    assert surface["message_id"] == answer["id"]
    assert surface["visual_complete"] is True
    assert not any(item["component"] == "Tabs" for item in _components(surface))
    assert surface["messages"][0]["createSurface"]["catalogId"] == REPORT_CATALOG_ID
    charts = [item for item in _components(surface) if item["component"] == "AustraBarChart"]
    assert charts[0]["mode"] == "single"
    assert charts[0]["rows"][0]["first"] == "8"
    assert any(item["component"] == "AustraDataTable" for item in _components(surface))
    assert not any(item["component"] == "Card" for item in _components(surface))


def test_inventory_table_keeps_every_column_and_charts_available_stock():
    body = (
        "Current raw material inventory\n\n"
        "| Material | Lots | On-hand (t) | Reserved (t) | QC-hold (t) | Available (t) |\n"
        "|---|---:|---:|---:|---:|---:|\n"
        "| Quartzite (QZ) | 4 | 115.0 | 15.0 | 20.0 | 80.0 |\n"
        "| Charcoal (CH) | 2 | 16.0 | 2.0 | 6.0 | 8.0 |\n\n"
        "**Lots currently on QC hold:**\n"
        "- RM-QZ-003 — 20.0 t quartzite, assay pending\n"
        "- RM-CH-002 — 6.0 t charcoal, moisture recheck pending\n\n"
        "These lots are excluded from available stock."
    )
    surface = Surface("inventory_test", REPORT_CATALOG_ID)
    components = surface.messages(table_root(surface, markdown_tables(body)))[-1][
        "updateComponents"
    ]["components"]
    chart = next(item for item in components if item["component"] == "AustraBarChart")
    table = next(item for item in components if item["component"] == "AustraDataTable")

    assert chart["title"] == "Available stock"
    assert chart["unit"] == "t"
    assert chart["rows"][0]["first"] == "80.0"
    assert chart["rows"][0]["details"] == [
        {"label": "Lots", "value": "4"},
        {"label": "On-hand (t)", "value": "115.0"},
        {"label": "Reserved (t)", "value": "15.0"},
        {"label": "QC-hold (t)", "value": "20.0"},
        {"label": "Available (t)", "value": "80.0"},
    ]
    assert table["headers"] == [
        "Material",
        "Lots",
        "On-hand (t)",
        "Reserved (t)",
        "QC-hold (t)",
        "Available (t)",
    ]
    assert table["rows"][1][0] == "Charcoal (CH)"
    hold_table = next(
        item
        for item in components
        if item["component"] == "AustraDataTable" and item["title"] == "Lots currently on QC hold"
    )
    assert hold_table["headers"] == ["Item", "Details"]
    assert hold_table["rows"][0] == ["RM-QZ-003", "20.0 t quartzite, assay pending"]
    assert any(
        item.get("text") == "These lots are excluded from available stock." for item in components
    )


def test_customer_and_supplier_tables_use_the_same_schema_independent_component():
    body = (
        "Customer contacts\n\n"
        "| Customer | City | Contact | Status |\n"
        "|---|---|---|---|\n"
        "| Acme | Delhi | Mira | Active |\n"
        "| Birch | Pune | Dev | Pending |\n\n"
        "Suppliers\n\n"
        "| Supplier | Material | Lead time |\n"
        "|---|---|---|\n"
        "| Cedar | Charcoal | 4 days |"
    )
    surface = Surface("contacts_test", REPORT_CATALOG_ID)
    components = surface.messages(table_root(surface, markdown_tables(body)))[-1][
        "updateComponents"
    ]["components"]
    tables = [item for item in components if item["component"] == "AustraDataTable"]

    assert len(tables) == 2
    assert tables[0]["headers"] == ["Customer", "City", "Contact", "Status"]
    assert tables[0]["rows"][0] == ["Acme", "Delhi", "Mira", "Active"]
    assert tables[1]["headers"] == ["Supplier", "Material", "Lead time"]
    assert not any(item["component"] == "AustraBarChart" for item in components)
    assert markdown_tables("No table here.") == []


def test_supplier_answer_has_three_data_rows_and_one_complete_visual_table():
    body = (
        "Here are the supplier candidates for quartz.\n\n"
        "| **Supplier ID** | **Name** | **City** | **Phone** |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| VND-QZ-01 | Quartzworks North | Jaipur | +91 00000 01001 |\n"
        "| VND-QZ-02 | Silica Valley Supply | Hubballi | +91 00000 01002 |\n"
        "| VND-QZ-03 | Mineral Link | Ahmedabad | +91 00000 01003 |\n\n"
        "Note: these are candidates, not approved vendors."
    )
    tables = markdown_tables(body)
    surface = Surface("supplier_test", REPORT_CATALOG_ID)
    components = surface.messages(table_root(surface, tables))[-1]["updateComponents"]["components"]
    visual_tables = [item for item in components if item["component"] == "AustraDataTable"]

    assert visual_is_complete(body, tables) is True
    assert len(visual_tables) == 1
    assert visual_tables[0]["headers"] == ["Supplier ID", "Name", "City", "Phone"]
    assert visual_tables[0]["rows"] == [
        ["VND-QZ-01", "Quartzworks North", "Jaipur", "+91 00000 01001"],
        ["VND-QZ-02", "Silica Valley Supply", "Hubballi", "+91 00000 01002"],
        ["VND-QZ-03", "Mineral Link", "Ahmedabad", "+91 00000 01003"],
    ]
    assert any(
        item.get("text") == "Note: these are candidates, not approved vendors."
        for item in components
    )


def test_visual_row_limit_keeps_all_rows_in_message_text(tmp_path):
    rows = "\n".join(f"| Material {index} | {index} |" for index in range(201))

    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply=f"Full inventory follows.\n\n| Material | Available (kg) |\n| --- | ---: |\n{rows}"
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    chat = client.post("/api/chats", json={"body": "Show the material balance table"}).json()[
        "chat"
    ]
    answer = chat["messages"][-1]
    assert "Material 200" in answer["body"]
    surface = client.get(f"/api/ui/chats/{chat['id']}").json()["surfaces"][0]
    assert surface["visual_complete"] is False
    components = _components(surface)
    assert any(
        item.get("text") == "Showing 200 of 201 rows. View the full answer." for item in components
    )
    chart = next(item for item in components if item["component"] == "AustraBarChart")
    assert len(chart["rows"]) == 20
    assert not any(item.get("text") == "Material 200" for item in components)


def test_report_mass_format_keeps_exact_decimal_digits():
    assert _tonnes("0") == "0 t"
    assert _tonnes("10000") == "10 t"
    assert _tonnes("0.000000123") == "0.000000000123 t"
