import pytest
from fastapi.testclient import TestClient

from austra.agent import CasePlan
from austra.knowledge_store import KnowledgeStore
from austra.store import Store
from austra.web import create_app


def plan(skill_body, document_ids=None):
    return CasePlan.model_validate(
        {
            "summary": "Create a reusable supplier check",
            "category": "purchasing",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "skill_drafts": [
                {"name": "supplier_check", "body": skill_body, "reason": "Repeated request"}
            ],
            "memory_drafts": [
                {
                    "content": "Supplier check requested by owner",
                    "reason": "Keep decision context",
                    "source_document_ids": document_ids or [],
                }
            ],
        }
    )


def test_skill_publication_and_memory_review(tmp_path):
    store = Store(tmp_path / "business.db")
    knowledge = KnowledgeStore(store)
    case = store.create_case("Create supplier check", "ui", "Owner")
    store.save_plan(case["id"], plan("# Supplier check\nVerify contacts.").model_dump(), [])
    drafts = knowledge.proposals_for_case(case["id"])
    skill = drafts["skill_drafts"][0]
    memory = drafts["memory_drafts"][0]

    assert knowledge.active_skills() == []
    assert knowledge.search_memory("Supplier check") == []
    with pytest.raises(KeyError):
        knowledge.read_skill("supplier_check")

    knowledge.review_skill(skill["id"], "approved", "Manager A")
    knowledge.review_memory(memory["id"], "approved", "Manager A")

    assert knowledge.read_skill("supplier_check")["version"] == 1
    assert (tmp_path / "skills" / "supplier_check" / "v1.md").read_text() == skill["body"]
    assert knowledge.search_memory("Supplier check")[0]["case_id"] == case["id"]

    store.save_plan(
        case["id"], plan("# Supplier check\nVerify contact and grade.").model_dump(), []
    )
    next_skill = knowledge.proposals_for_case(case["id"])["skill_drafts"][-1]
    assert "-Verify contacts." in next_skill["diff"]
    assert "+Verify contact and grade." in next_skill["diff"]
    knowledge.review_skill(next_skill["id"], "approved", "Manager B")

    assert knowledge.read_skill("supplier_check")["version"] == 2
    assert len(knowledge.active_skills()) == 1
    with pytest.raises(ValueError, match="already been reviewed"):
        knowledge.review_skill(next_skill["id"], "approved", "Manager B")

    (tmp_path / "skills" / "supplier_check" / "v2.md").write_text("Changed outside review")
    with pytest.raises(ValueError, match="integrity"):
        knowledge.read_skill("supplier_check")


def test_memory_draft_requires_existing_document(tmp_path):
    store = Store(tmp_path / "business.db")
    case = store.create_case("Remember a quote", "ui", "Owner")

    with pytest.raises(ValueError, match="unknown document"):
        store.save_plan(case["id"], plan("# Skill", ["missing-id"]).model_dump(), [])

    assert store.get_case(case["id"])["status"] == "new"


def test_review_api_publishes_only_with_manager_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AUSTRA_MANAGER_TOKEN", "test-secret")
    client = TestClient(
        create_app(
            tmp_path / "business.db",
            planner=lambda _text, _policies, _references: plan("# Supplier check"),
        )
    )
    case = client.post("/api/cases", json={"message": "Create a supplier check"}).json()
    detail = client.post(f"/api/cases/{case['id']}/analyze").json()
    skill_id = detail["skill_drafts"][0]["id"]

    denied = client.post(
        f"/api/proposals/skills/{skill_id}/review",
        json={"decision": "approved", "manager": "Manager A"},
    )
    allowed = client.post(
        f"/api/proposals/skills/{skill_id}/review",
        json={"decision": "approved", "manager": "Manager A"},
        headers={"X-Manager-Token": "test-secret"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert client.get("/api/skills").json()[0]["name"] == "supplier_check"


def test_followup_message_invalidates_unreviewed_knowledge(tmp_path):
    store = Store(tmp_path / "business.db")
    knowledge = KnowledgeStore(store)
    case = store.create_case("Draft a skill", "ui", "Owner")
    store.save_plan(case["id"], plan("# Supplier check").model_dump(), [])
    store.add_message(case["id"], "Actually, use a different rule", "Owner")

    drafts = knowledge.proposals_for_case(case["id"])
    assert drafts["skill_drafts"][0]["status"] == "superseded"
    assert drafts["memory_drafts"][0]["status"] == "superseded"
    with pytest.raises(ValueError, match="already been reviewed"):
        knowledge.review_skill(drafts["skill_drafts"][0]["id"], "approved", "Manager")


def test_system_change_is_recorded_without_modifying_runtime(tmp_path):
    store = Store(tmp_path / "business.db")
    knowledge = KnowledgeStore(store)
    case = store.create_case("Add a workflow state", "ui", "Owner")
    proposed = CasePlan.model_validate(
        {
            "summary": "Add a manager review state",
            "category": "other",
            "missing_information": [],
            "actions": [],
            "record_types": [],
            "system_changes": [
                {
                    "area": "workflow",
                    "title": "Add manager review",
                    "specification": "Transition from review after manager approval.",
                    "reason": "Current workflow has no approval transition.",
                }
            ],
        }
    )
    store.save_plan(case["id"], proposed.model_dump(), [])
    change = knowledge.proposals_for_case(case["id"])["system_changes"][0]

    knowledge.review_system_change(change["id"], "approved", "Manager")

    assert knowledge.proposals_for_case(case["id"])["system_changes"][0]["status"] == "approved"
    assert store.workflow.target("new", "plan_proposed") == "review"
