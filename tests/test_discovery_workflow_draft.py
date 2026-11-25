"""Discovery manager assigns the planner after consequential decisions are resolved."""

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import ValidationError

from austra.integrations.model_credentials import ModelCredentials
from austra.onboarding import routes
from austra.onboarding.decisions import unique_decisions
from austra.onboarding.discovery import (
    DRAFT_SYSTEM,
    DiscoveryTurn,
    DraftExample,
    WorkflowDraft,
    candidate_proposal,
    clarification_reply,
    grounded_discovery_turn,
    pending_workflow_draft,
    planning_assignment,
    reconcile_saved_draft_reply,
    workflow_draft_requested,
)
from austra.platform.auth import password_hash
from austra.skills import load_skill
from austra.web import create_app


def test_saved_draft_reply_uses_persisted_decision_count_and_pending_source_state():
    turn = DiscoveryTurn(
        reply=(
            "I've logged this note as supporting evidence for the existing draft. "
            "The same four open decisions remain. Resolve those four open decisions now?"
        ),
        working_summary="",
        unresolved_questions=[],
    )
    draft = {"name": "Pilot release", "open_decisions": [f"Decision {i}" for i in range(5)]}
    grounded = reconcile_saved_draft_reply(turn, draft)
    assert "pending source review" in grounded.reply
    assert "five open decisions" in grounded.reply
    assert "four open decisions" not in grounded.reply
    assert reconcile_saved_draft_reply(turn, None) == turn


def test_explicit_request_survives_one_clarification():
    history = [
        {"role": "user", "body": "Please create a workflow draft now."},
        {"role": "assistant", "body": "Who decides if an enquiry is qualified?"},
    ]
    assert workflow_draft_requested(history[0]["body"])
    assert pending_workflow_draft("Only I will decide.", history, {})
    assert not pending_workflow_draft("Stop the draft.", history, {})
    assert not pending_workflow_draft(
        "Only I will decide.", history, {"workflow_candidate_id": "saved"}
    )
    assert not workflow_draft_requested("Do not create a workflow draft yet.")
    assert not workflow_draft_requested("Do not revise the saved draft yet.")
    assert workflow_draft_requested(
        "Please revise the saved draft. Don't approve, build, connect, or send anything."
    )
    assert workflow_draft_requested(
        "Please save a draft for review. Do not mark the draft approved."
    )
    assert not pending_workflow_draft("Did you save a draft?", history, {})
    assert not pending_workflow_draft("Do not draft it yet.", history, {})
    assert workflow_draft_requested("pause discovery and start making solution")
    assert workflow_draft_requested("Please revise the saved Client Incident Handoff draft.")
    assert workflow_draft_requested("Update the workflow draft for review.")
    assert pending_workflow_draft("pause discovery and start making solution", [], {})
    assert pending_workflow_draft(
        "Please revise the saved Client Incident Handoff draft.",
        [], {"workflow_candidate_id": "saved"},
    )


def test_candidate_proposal_keeps_open_decisions_for_manager_gate():
    draft = WorkflowDraft(
        steps=["Harshil decides if the enquiry is qualified."],
        outputs=["Reviewable enquiry status"],
        open_decisions=["What counts as signed?"],
        resolved_questions=["Who qualifies an enquiry?"],
        acceptance_examples=[
            DraftExample(category=kind, input="Fictional enquiry", expected="Needs review")
            for kind in ("normal", "boundary", "adverse")
        ],
    )
    proposal = candidate_proposal(
        draft, ["Who qualifies an enquiry?", "Who may approve a billing exception?"]
    )
    assert proposal["behavior"]["actions"] == []
    assert proposal["behavior"]["manual_steps"] == draft.steps
    assert proposal["open_decisions"] == [
        "Who may approve a billing exception?", "What counts as signed?"
    ]


def test_candidate_proposal_names_draft_and_removes_stale_candidate_references():
    stale_id = str(uuid4())
    draft = WorkflowDraft(
        name="Client enquiry handoff",
        steps=[f"Review candidate {stale_id} with sales."],
        outputs=["Sales handoff status"],
        open_decisions=["Who approves an outage statement?"],
        resolved_questions=[],
        acceptance_examples=[
            DraftExample(category=kind, input="Client enquiry", expected="Human review")
            for kind in ("normal", "boundary", "adverse")
        ],
    )
    proposal = candidate_proposal(draft, ["Who is the outage statement approver?"])
    assert proposal["name"] == "Client enquiry handoff"
    assert stale_id not in str(proposal)
    assert len(proposal["open_decisions"]) == 1


def test_scope_change_decisions_collapse_paraphrases_without_losing_topics():
    questions = [
        "What system is the source of truth for the change request?",
        "Is the project tracker the authoritative record for that change request?",
        "Does the account owner have an approval threshold for a change?",
        "Who is the backup account owner for a change approval?",
        "Who performs the human review before sending to the client?",
        "Who reviews a client message before it goes out?",
        "Is a signed change order required before work begins?",
        "Whether a signed change order is required remains unresolved.",
    ]
    assert len(unique_decisions(questions)) == 4
    assert unique_decisions(questions, [questions[0]]) == [questions[2], questions[4], questions[6]]


def test_incident_decisions_collapse_run_five_paraphrases():
    questions = [
        "What is the urgent-versus-routine severity threshold?",
        "Who may approve an outage statement to the client?",
        "Who takes over if the assigned engineer is unavailable?",
        "How should sensitive client logs be handled during incident handling?",
        "Who sent the forwarded override note and was any real approval given?",
        "Who holds approval authority to issue an outage statement to the client?",
        "What is the backup or escalation path when the assigned engineer is unavailable?",
        "Who sent the forwarded note claiming prior approvals, and was any real approval given?",
    ]
    assert len(unique_decisions(questions)) == 5
    assert "excluded a source and said to set" in DRAFT_SYSTEM


def test_discovery_does_not_repeat_false_draft_capability_denial():
    turn = DiscoveryTurn(
        reply="I still cannot build, save, or activate a workflow from this chat.",
        working_summary="",
        unresolved_questions=["What counts as signed?"],
    )
    assert "open decisions clearly marked" in grounded_discovery_turn(turn).reply


def test_manager_assignment_saves_explicit_draft_with_open_decisions():
    turn = DiscoveryTurn(
        reply="I can help simplify the handoff.", working_summary="",
        unresolved_questions=[], next_task="plan", planning_brief="Order intake needs review.",
    )
    assert planning_assignment(turn, ["Who approves?"], False, False, False) == "clarify"
    assert planning_assignment(turn, [], False, False, False) == "plan"
    assert planning_assignment(turn, [], False, True, False) == "continue"
    assert planning_assignment(turn, [], False, False, True) == "continue"
    assert planning_assignment(turn, [], True, True, False) == "plan"
    turn.resolved_questions = ["Who approves?"]
    assert planning_assignment(turn, ["Who approves?"], False, False, False) == "plan"
    assert "1. Who approves?" in clarification_reply(["Who approves?"])
    assert "can save an unapproved draft" in clarification_reply(["Who approves?"])
    turn.next_task = "clarify"
    assert planning_assignment(turn, [], True, False, False) == "plan"
    assert planning_assignment(turn, ["Who approves?"], True, False, False) == "plan"


def test_planning_assignment_requires_a_brief_and_selected_skill():
    with pytest.raises(ValidationError, match="planning assignment needs a brief"):
        DiscoveryTurn(
            reply="I will plan it.", working_summary="", unresolved_questions=[],
            next_task="plan", planning_brief="",
        )
    assert "review_state: reviewed" in load_skill("planning-workflow-solutions")
    assert "skill grants no tool rights" in load_skill("planning-workflow-solutions")


def test_chat_saves_unapproved_draft_with_open_decisions(tmp_path, monkeypatch):
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"draft_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "test platform password 123")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "legacy")
            monkeypatch.setenv("AUSTRA_GOOGLE_WORKSPACE_CLIENT_ID", "fixture-client")
            monkeypatch.setenv("AUSTRA_GOOGLE_WORKSPACE_CLIENT_SECRET", "fixture-client-secret")
            monkeypatch.setattr(ModelCredentials, "get", lambda self, org_id: "fixture-key")
            monkeypatch.setattr(routes, "extract_memory_update", lambda *args, **kwargs: None)
            monkeypatch.setattr(routes, "save_memory_update", lambda *args, **kwargs: None)
            app = create_app(tmp_path)
            with psycopg.connect(database_url) as db:
                organization_id = db.execute(
                    "INSERT INTO organizations (slug, name) VALUES ('acme', 'Acme') RETURNING id"
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, "
                    "password_hash, must_change_password) VALUES (%s, 'admin', 'org_admin', "
                    "%s, false)",
                    (organization_id, password_hash("fixture admin password")),
                )
                db.execute(
                    "INSERT INTO organization_tours (organization_id, about_admin, "
                    "about_organization, completed_at, revision, discovery_state) "
                    "VALUES (%s, 'Ava runs operations', 'Acme makes bikes', now(), 1, %s)",
                    (
                        organization_id,
                        psycopg.types.json.Jsonb({
                            "unresolved_questions": [
                                "Who qualifies an enquiry?", "What counts as signed?"
                            ]
                        }),
                    ),
                )
                db.execute(
                    "INSERT INTO onboarding_messages (organization_id, position, role, body) "
                    "VALUES (%s, 1, 'user', 'Please create a workflow draft now.'), "
                    "(%s, 2, 'assistant', 'Who decides whether an enquiry is qualified?')",
                    (organization_id, organization_id),
                )
                case_id = db.execute(
                    "INSERT INTO onboarding_cases (organization_id, created_event_id) "
                    "VALUES (%s, %s) RETURNING id",
                    (organization_id, uuid4()),
                ).fetchone()[0]
            draft_with_gap = WorkflowDraft(
                steps=[
                    "Record the enquiry as a separate proposed project.",
                    "Harshil alone decides whether the enquiry is qualified.",
                    "Hold the won decision for a named human review.",
                ],
                outputs=["Sales and finance see the proposed project"],
                open_decisions=["Who can declare a deal won?"],
                resolved_questions=[],
                acceptance_examples=[
                    DraftExample(
                        category=kind,
                        input=f"Fictional {kind} enquiry",
                        expected="Human review before a won decision",
                    )
                    for kind in ("normal", "boundary", "adverse")
                ],
            )
            planner_calls = []

            def plan(*args, **kwargs):
                planner_calls.append(args[3]["manager_brief"])
                if len(planner_calls) == 1:
                    return draft_with_gap
                return draft_with_gap.model_copy(update={"open_decisions": []})

            def manage(*args, **kwargs):
                body = args[3][-1]["body"]
                if body == "Only I will decide.":
                    return DiscoveryTurn(
                        reply="Who can declare a deal won?", working_summary="",
                        unresolved_questions=["What counts as signed?"],
                        resolved_questions=["Who qualifies an enquiry?"],
                    )
                if body == "Signed means the client accepts the scope in writing.":
                    return DiscoveryTurn(
                        reply="I will plan the workflow.", working_summary="",
                        unresolved_questions=[], resolved_questions=["What counts as signed?"],
                        next_task="plan", planning_brief="Written scope acceptance starts planning.",
                    )
                return DiscoveryTurn(
                    reply="I will update the plan.", working_summary="",
                    unresolved_questions=[], resolved_questions=["Who can declare a deal won?"],
                    next_task="plan", planning_brief="Harshil declares the deal won.",
                )

            monkeypatch.setattr(routes, "generate_workflow_draft", plan)
            monkeypatch.setattr(routes, "generate_discovery_turn", manage)
            client = TestClient(app, base_url="http://acme.localhost:8000")
            login = client.post(
                "/api/login",
                json={"username": "admin", "password": "fixture admin password"},
            )
            assert login.status_code == 200
            result = client.post(
                "/api/org/discovery/message",
                headers={"X-Austra-Request": "1"},
                json={"body": "Only I will decide.", "revision": 1},
            )
            assert result.status_code == 200, result.text
            first = result.json()
            candidate_id = first["discovery_state"]["workflow_candidate_id"]
            assert candidate_id not in first["messages"][-1]["body"]
            assert "Workflow draft" in first["messages"][-1]["body"]
            assert "confirmation is blocked" in first["messages"][-1]["body"]
            assert planner_calls == [""]
            second = client.post(
                "/api/org/discovery/message", headers={"X-Austra-Request": "1"},
                json={
                    "body": "Signed means the client accepts the scope in writing.",
                    "revision": first["revision"],
                },
            )
            assert second.status_code == 200, second.text
            pending = second.json()
            assert pending["discovery_state"]["workflow_candidate_id"] == candidate_id
            assert planner_calls == [""]
            with psycopg.connect(database_url) as db:
                assert db.execute("SELECT count(*) FROM blueprint_candidates").fetchone()[0] == 1
            third = client.post(
                "/api/org/discovery/message",
                headers={"X-Austra-Request": "1"},
                json={
                    "body": "Please create a revised workflow draft: Harshil alone declares the deal won.",
                    "revision": pending["revision"],
                },
            )
            assert third.status_code == 200, third.text
            saved = third.json()
            revised_id = saved["discovery_state"]["workflow_candidate_id"]
            assert revised_id not in saved["messages"][-1]["body"]
            assert planner_calls[-1] == "Harshil declares the deal won."
            review = client.get(f"/api/org/onboarding-cases/{case_id}/candidate")
            assert review.status_code == 200, review.text
            candidate = review.json()
            assert str(candidate["id"]) == revised_id
            assert candidate["behavior"]["actions"] == []
            assert candidate["behavior"]["manual_steps"] == draft_with_gap.steps
            assert candidate["open_decisions"] == []
            with psycopg.connect(database_url) as db:
                assert db.execute("SELECT count(*) FROM blueprint_candidates").fetchone()[0] == 2
                assert db.execute("SELECT count(*) FROM blueprints").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM integration_connections").fetchone()[0] == 0

            def separate_draft(_key, _org, history, state, **_kwargs):
                assert len(history) == 1
                assert "pause the batch" in history[0]["body"]
                assert not state["unresolved_questions"]
                assert not state["saved_draft"]
                return draft_with_gap.model_copy(update={"name": "Deviation review", "open_decisions": []})

            monkeypatch.setattr(routes, "generate_workflow_draft", separate_draft)
            separate = client.post("/api/org/discovery/message", headers={"X-Austra-Request": "1"},
                json={"body": "Create a second, separate workflow draft for deviation review. "
                      "When a reading exceeds the limit, pause the batch.",
                      "revision": saved["revision"]})
            assert separate.status_code == 200, separate.text
            cases = client.get("/api/org/onboarding-cases/current").json()["workflows"]
            assert {item["name"] for item in cases} == {"Workflow draft", "Deviation review"}
            assert client.get(f"/api/org/onboarding-cases/{case_id}/candidate").json()["id"] == revised_id

            def resume(_key, _org, _description, history, state, **_kwargs):
                assert any("declares the deal won" in item["body"] for item in history)
                assert all("pause the batch" not in item["body"] for item in history)
                assert state["saved_draft"]["name"] == "Workflow draft"
                return DiscoveryTurn(
                    reply="I need access to the stock sheet.",
                    working_summary="", unresolved_questions=[],
                    google_workspace_required="Google Sheets",
                )

            monkeypatch.setattr(routes, "generate_discovery_turn", resume)
            resumed = client.post("/api/org/discovery/message", headers={"X-Austra-Request": "1"},
                json={"body": "Can you read the stock sheet?", "revision": separate.json()["revision"],
                      "workflow_case_id": str(case_id)})
            assert resumed.status_code == 200, resumed.text
            messages = resumed.json()["messages"]
            assert all("workspace_consent" not in message for message in messages[:-1])
            consent = messages[-1]["workspace_consent"]
            assert consent["kind"] == "workspace_consent"
            assert consent["message_id"] == messages[-1]["id"]
            assert consent["workflow_id"] == str(case_id)
            assert consent["app"] == "Google Sheets"
            assert client.get("/api/org/tour").json()["messages"][-1]["workspace_consent"] == consent
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
