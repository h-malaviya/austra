from fastapi.testclient import TestClient

from austra.agent import CasePlan
from austra.web import create_app


def test_manager_gate_and_proposed_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTRA_MANAGER_TOKEN", "test-secret")
    plan = CasePlan.model_validate(
        {
            "summary": "Need quartz purchasing review",
            "category": "purchasing",
            "missing_information": ["quantity"],
            "actions": [
                {
                    "kind": "ask_question",
                    "description": "Ask quantity",
                    "reason": "Unknown quantity",
                }
            ],
            "record_types": [],
        }
    )
    client = TestClient(
        create_app(tmp_path / "business.db", planner=lambda _text, _rules, _references: plan)
    )

    case = client.post("/api/cases", json={"message": "Need quartz", "sender": "Owner"}).json()
    denied = client.post("/api/policies", json={"title": "Approvals", "body": "Manager approves"})
    analyzed = client.post(f"/api/cases/{case['id']}/analyze")

    assert denied.status_code == 403
    assert analyzed.status_code == 200
    assert analyzed.json()["actions"][0]["status"] == "proposed"
    assert len(analyzed.json()["events"][-1]["details"]["policy_ids"]) == 1

    allowed = client.post(
        "/api/policies",
        json={"title": "Approvals", "body": "Manager approves"},
        headers={"X-Manager-Token": "test-secret"},
    )
    assert allowed.status_code == 201
    assert allowed.json()["status"] == "draft"


def test_business_api_rejects_remote_clients(tmp_path):
    app = create_app(tmp_path / "business.db")
    remote = TestClient(app, client=("203.0.113.10", 5000))

    assert remote.get("/api/cases").status_code == 403


def test_chat_assets_and_recent_conversation_order(tmp_path):
    client = TestClient(create_app(tmp_path / "business.db"))
    first = client.post("/api/cases", json={"message": "First chat"}).json()
    second = client.post("/api/cases", json={"message": "Second chat"}).json()
    client.post(f"/api/cases/{first['id']}/messages", json={"message": "Update first chat"})

    assert "New chat" in client.get("/").text
    assert "renderWelcome" in client.get("/static/ui.js").text
    assert "sendMessage" in client.get("/static/app.js").text
    assert client.get("/static/app.css").status_code == 200
    assert [item["id"] for item in client.get("/api/cases").json()] == [first["id"], second["id"]]


def test_analysis_receives_indexed_documents_as_references(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTRA_MANAGER_TOKEN", "test-secret")
    seen = {}

    def planner(text, policies, references):
        seen["conversation"] = text
        seen["policies"] = policies
        seen["references"] = references
        return CasePlan.model_validate(
            {
                "summary": "Supplier review",
                "category": "purchasing",
                "missing_information": [],
                "actions": [],
                "record_types": [],
            }
        )

    client = TestClient(create_app(tmp_path / "business.db", planner=planner))
    case = client.post("/api/cases", json={"message": "Find quartz supplier"}).json()
    client.post(
        f"/api/cases/{case['id']}/messages",
        json={"message": "Please use the uploaded supplier list", "sender": "Manager A"},
    )
    uploaded = client.post(
        "/api/admin/documents",
        headers={"X-Manager-Token": "test-secret"},
        data={"uploaded_by": "Manager A"},
        files={"file": ("suppliers.csv", b"name,material\nAcme,quartz\n", "text/csv")},
    ).json()

    analyzed = client.post(f"/api/cases/{case['id']}/analyze").json()

    assert seen["references"][0]["id"] == uploaded["id"]
    assert "uploaded supplier list" in seen["conversation"]
    assert "USD 7,000" in seen["policies"][0]["body"]
    assert analyzed["events"][-1]["details"]["document_ids"] == [uploaded["id"]]
