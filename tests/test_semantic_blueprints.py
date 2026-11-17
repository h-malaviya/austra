"""Phase 2 graph, citation, compiler, and confirmation gates in isolated tenants."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from austra.build_release.monitoring import list_reviews, triage_feedback
from austra.build_release.service import create_build, publish, transition_build
from austra.onboarding.evidence import process_job, save_file, save_website_page
from austra.onboarding.service import open_case
from austra.semantics.service import (
    add_edge,
    add_revision,
    answer_question,
    confirm_candidate,
    decide_claim,
    extract_claims,
    propose_candidate,
    record_conflict,
    review_candidate,
)
from austra.skills import PLATFORM_SKILLS, load_skill, load_skills, semantic_skill_cards
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"semantics_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def organization(connection, slug):
    org_id = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id",
        (slug, slug),
    ).fetchone()[0]
    user_id = connection.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'test', false) RETURNING id",
        (org_id,),
    ).fetchone()[0]
    event_id = connection.execute(
        "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
        "outcome, correlation_id) VALUES (%s, %s, 'test', 'organization.created', "
        "'success', %s) RETURNING id", (org_id, user_id, uuid4()),
    ).fetchone()[0]
    actor = TenantContext.from_auth(AuthContext(user_id, org_id, "org_admin", True, True))
    return actor, open_case(connection, org_id, event_id)


def source_claim(connection, actor, root, filename="operating.txt"):
    source = save_file(connection, actor, root, filename,
                       b"A purchasing manager may approve orders up to 100 USD.")
    process_job(connection, actor, root, source["revision_id"])
    with tenant_transaction(connection, actor):
        chunk_id = connection.execute(
            "SELECT id FROM evidence_chunks WHERE organization_id = %s AND revision_id = %s",
            (actor.organization_id, source["revision_id"]),
        ).fetchone()[0]
    claim_id = extract_claims(connection, actor, chunk_id)[0]["id"]
    decide_claim(connection, actor, claim_id, "confirmed", "Administrator verified operating rule")
    return claim_id


def proposal(revision_ids, edge_ids=()):
    return {"revision_ids": list(revision_ids), "edge_ids": list(edge_ids),
            "behavior": {"reads": [], "decisions": [],
                         "actions": [], "approvers": [],
                         "manual_steps": ["review"], "outputs": ["decision"]},
            "acceptance_examples": [
                {"category": category, "input": f"sample {category}", "expected": "review"}
                for category in ("normal", "boundary", "adverse")
            ]}


def revision(connection, actor, claim_id, kind, key, content, *, start=None, end=None, owner=True):
    return add_revision(
        connection, actor, kind=kind, stable_key=key, content=content,
        rationale="Confirmed operating evidence", claim_ids=[claim_id],
        owner_user_id=actor.actor.user_id if owner else None,
        valid_from=start or datetime(2026, 1, 1, tzinfo=UTC), valid_to=end,
    )["id"]


def test_meaning_change_confirms_next_version_without_reembedding_old_revision(database, tmp_path):
    actor, case_id = organization(database, "versioned-feedback")
    claim_id = source_claim(database, actor, tmp_path)
    original = revision(database, actor, claim_id, "knowledge", "purchase_note",
                        {"summary": "Review purchase orders"})
    first = propose_candidate(database, actor, case_id, proposal([original]))
    first_blueprint = confirm_candidate(database, actor, case_id, first["content_hash"])
    build = create_build(database, actor, first_blueprint["blueprint_id"])
    build_revision = 1
    for state in ("testing", "admin_preview", "accepted", "ready_for_release"):
        build_revision = transition_build(database, actor, build["id"], state,
                                          build_revision, uuid4())
    release_id = publish(database, actor, build["id"], build_revision, uuid4())
    changed = save_file(database, actor, tmp_path, "operating.txt",
                        b"A purchasing manager now reviews each exception before approval.")
    assert changed["changed"] is True
    assert "changed_evidence" in {case["trigger"] for case in list_reviews(database, actor)}
    feedback = triage_feedback(
        database, actor, release_id, "business_meaning_change", "new instruction",
        "existing instruction followed", [{"claim_id": str(claim_id)}],
    )
    additional = revision(database, actor, claim_id, "knowledge", "purchase_exception",
                          {"summary": "Clarified purchase exception"})
    second = propose_candidate(database, actor, feedback["clarification_case_id"],
                               proposal([original, additional]))
    next_blueprint = confirm_candidate(database, actor, feedback["clarification_case_id"],
                                       second["content_hash"])
    assert next_blueprint["version"] == 2
    assert database.execute(
        "SELECT count(*) FROM semantic_revision_embeddings WHERE organization_id = %s "
        "AND revision_id = %s", (actor.organization_id, original),
    ).fetchone()[0] == 1
    assert database.execute(
        "SELECT id FROM releases WHERE organization_id = %s AND state = 'active'",
        (actor.organization_id,),
    ).fetchone()[0] == release_id


def test_review_hash_confirmation_and_tenant_scope(database, tmp_path):
    actor, case_id = organization(database, "first")
    other, other_case = organization(database, "second")
    claim_id = source_claim(database, actor, tmp_path)
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "review order"})
    draft = propose_candidate(database, actor, case_id, proposal([capability]))
    assert draft["state"] == "ready_for_review"
    review = review_candidate(database, actor, case_id)
    assert review["revisions"][0]["sources"][0]["claim_id"] == claim_id
    assert review["behavior"]["outputs"] == ["decision"]
    assert review_candidate(database, other, case_id) is None
    with pytest.raises(ValueError, match="hash mismatch"):
        confirm_candidate(database, actor, case_id, "a" * 64)
    amended = proposal([capability])
    amended["behavior"]["outputs"] = ["approval queue"]
    newer = propose_candidate(database, actor, case_id, amended)
    with pytest.raises(ValueError, match="hash mismatch"):
        confirm_candidate(database, actor, case_id, draft["content_hash"])
    employee = TenantContext.from_auth(AuthContext(uuid4(), actor.organization_id,
                                                   "employee", True, True))
    with pytest.raises(PermissionError):
        confirm_candidate(database, employee, case_id, newer["content_hash"])
    cross_tenant = propose_candidate(database, other, other_case, proposal([capability]))
    assert "broken_link" in {issue["type"] for issue in cross_tenant["issues"]}
    locked = confirm_candidate(database, actor, case_id, newer["content_hash"])
    assert locked["content_hash"] == newer["content_hash"]
    with pytest.raises(ValueError, match="not ready"):
        confirm_candidate(database, actor, case_id, newer["content_hash"])
    with tenant_transaction(database, other):
        assert database.execute("SELECT revision_id FROM blueprint_members").fetchall() == []
    with tenant_transaction(database, actor):
        assert database.execute(
            "SELECT count(*) FROM blueprint_manifests WHERE blueprint_id = %s",
            (locked["blueprint_id"],),
        ).fetchone()[0] == 1


def test_summary_cannot_add_unlinked_action(database, tmp_path):
    actor, case_id = organization(database, "unlinked")
    claim_id = source_claim(database, actor, tmp_path)
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    proposed = proposal([capability])
    proposed["behavior"]["actions"] = ["gmail.send_email"]
    blocked = propose_candidate(database, actor, case_id, proposed)
    assert {issue["type"] for issue in blocked["issues"]} >= {"broken_link", "side_effect"}


def test_conflicts_overlap_temporal_rules_and_missing_owner(database, tmp_path):
    actor, case_id = organization(database, "rules")
    claim_id = source_claim(database, actor, tmp_path)
    actor_revision = revision(database, actor, claim_id, "actor", "manager",
                              {"role": "manager", "platform_role": "employee"})
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    start = datetime(2026, 1, 1, tzinfo=UTC)
    common = {"decision_key": "approve_purchase", "scope": "all", "effect": "approve",
              "limit": "100", "unit": "USD", "required_permission": "workflows.run"}
    first = revision(database, actor, claim_id, "authority", "first", common,
                     start=start, end=start + timedelta(days=10))
    second = revision(database, actor, claim_id, "authority", "second", common,
                      start=start + timedelta(days=5))
    missing = revision(database, actor, claim_id, "knowledge", "note", {"text": "note"}, owner=False)
    edges = [add_edge(database, actor, first, "authorizes", actor_revision, "Manager") ,
             add_edge(database, actor, first, "constrains", capability, "Purchase"),
             add_edge(database, actor, second, "authorizes", actor_revision, "Manager"),
             add_edge(database, actor, second, "constrains", capability, "Purchase")]
    draft = propose_candidate(database, actor, case_id,
                              proposal([actor_revision, capability, first, second, missing], edges))
    assert {issue["type"] for issue in draft["issues"]} >= {"overlapping_rule", "owner"}
    answered = answer_question(database, actor, case_id, 0, "Only the newer approved limit applies")
    assert answered["content_hash"] != draft["content_hash"]
    review = review_candidate(database, actor, case_id)
    assert review["changes"] == ["clarifications"]
    assert review["clarifications"][-1]["answer"] == "Only the newer approved limit applies"
    assert "overlapping_rule" in {issue["type"] for issue in answered["issues"]}
    with pytest.raises(ValueError, match="not ready"):
        confirm_candidate(database, actor, case_id, answered["content_hash"])
    with pytest.raises(ValueError, match="Relationship endpoints"):
        add_edge(database, actor, capability, "authorizes", actor_revision, "Wrong")


def test_claim_conflict_and_marketing_do_not_promote(database, tmp_path):
    actor, case_id = organization(database, "marketing")
    first = source_claim(database, actor, tmp_path)
    second = source_claim(database, actor, tmp_path, "second.txt")
    record_conflict(database, actor, first, second, "Different statements")
    one = revision(database, actor, first, "capability", "one", {"outcome": "one"})
    two = revision(database, actor, second, "capability", "two", {"outcome": "two"})
    draft = propose_candidate(database, actor, case_id, proposal([one, two]))
    assert "conflicting_claims" in {issue["type"] for issue in draft["issues"]}
    question = next(item for item in draft["questions"] if "conflicting_claims" in item["question"])
    assert {source["claim_id"] for source in question["citations"]} == {first, second}
    website = save_website_page(database, actor, tmp_path,
                                "https://example.org/marketing", b"<p>We approve every order.</p>")
    process_job(database, actor, tmp_path, website["revision_id"])
    with tenant_transaction(database, actor):
        chunk_id = database.execute(
            "SELECT id FROM evidence_chunks WHERE revision_id = %s",
            (website["revision_id"],),
        ).fetchone()[0]
    unconfirmed = extract_claims(database, actor, chunk_id)[0]["id"]
    pending = revision(database, actor, unconfirmed, "capability", "pending", {"outcome": "claim"})
    blocked = propose_candidate(database, actor, case_id, proposal([pending]))
    assert "unconfirmed_claim" in {issue["type"] for issue in blocked["issues"]}
    decide_claim(database, actor, unconfirmed, "confirmed", "Reviewed public claim")
    with pytest.raises(ValueError, match="marketing claims"):
        revision(database, actor, unconfirmed, "policy", "marketing_rule",
                 {"decision_key": "approve", "scope": "all", "effect": "allow"})


def test_invalid_transition_scope_and_nonoverlapping_authority(database, tmp_path):
    actor, case_id = organization(database, "constraints")
    claim_id = source_claim(database, actor, tmp_path)
    workflow = revision(database, actor, claim_id, "workflow", "orders",
                        {"states": ["draft", "approved"],
                         "transitions": [{"from": "draft", "to": "missing"}]})
    integration = revision(database, actor, claim_id, "integration", "mail",
                           {"requested_scopes": ["mail.send"],
                            "supported_scopes": ["mail.read"], "action_ids": ["send"]})
    draft = propose_candidate(database, actor, case_id, proposal([workflow, integration]))
    assert {issue["type"] for issue in draft["issues"]} >= {"transition", "integration_scope"}

    start = datetime(2026, 1, 1, tzinfo=UTC)
    manager = revision(database, actor, claim_id, "actor", "manager",
                       {"role": "manager", "platform_role": "employee"})
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    rule = {"decision_key": "approve", "scope": "orders", "effect": "allow",
            "limit": "100", "unit": "USD", "required_permission": "workflows.run"}
    first = revision(database, actor, claim_id, "authority", "old_limit", rule,
                     start=start, end=start + timedelta(days=10))
    second = revision(database, actor, claim_id, "authority", "new_limit", rule,
                      start=start + timedelta(days=10))
    edges = [add_edge(database, actor, authority, relation, target, "Confirmed")
             for authority in (first, second)
             for relation, target in (("authorizes", manager), ("constrains", capability))]
    revised = propose_candidate(database, actor, case_id,
                                proposal([manager, capability, first, second], edges))
    assert not revised["issues"]
    locked = confirm_candidate(database, actor, case_id, revised["content_hash"])
    with tenant_transaction(database, actor):
        manifest = database.execute(
            "SELECT manifest FROM blueprint_manifests WHERE blueprint_id = %s",
            (locked["blueprint_id"],),
        ).fetchone()[0]
    assert manifest["sources"][str(first)][0]["claim_id"] == str(claim_id)


def test_semantic_skills_are_reviewed_and_legacy_loader_is_unchanged():
    cards = semantic_skill_cards()
    assert {card.skill_id for card in cards} == {"eliciting-requirements", "modeling-organization"}
    assert all(len(card.content_hash) == 64 for card in cards)
    for card in cards:
        body = load_skill(card.skill_id)
        assert "review_state: reviewed" in body
        assert "skill grants no tool rights" in body.lower()
    assert load_skills() == "\n\n".join(load_skill(name) for name in PLATFORM_SKILLS)
    assert "never promote marketing" in load_skill("eliciting-requirements").lower()
