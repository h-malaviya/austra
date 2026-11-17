"""Labeled Phase 3 retrieval evaluation in disposable tenants."""

import logging
import os
from datetime import UTC, datetime
from statistics import median, quantiles
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from austra.onboarding.evidence import process_job, save_file
from austra.retrieval.context import INPUT_HARD_CAP, INPUT_TARGET, answer_packet
from austra.retrieval.service import retrieve
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant

logger = logging.getLogger(__name__)


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"retrieval_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def organization(connection, slug):
    org = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id", (slug, slug)
    ).fetchone()[0]
    user = connection.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'test', false) RETURNING id",
        (org,),
    ).fetchone()[0]
    case = connection.execute(
        "INSERT INTO onboarding_cases (organization_id, created_event_id) VALUES (%s, %s) "
        "RETURNING id", (org, uuid4())
    ).fetchone()[0]
    return TenantContext.from_auth(AuthContext(user, org, "org_admin", True, True)), case


def approved_source(connection, actor, case, root, name, content, *, version=1, key="returns_policy"):
    source = save_file(connection, actor, root, name, content.encode())
    while process_job(connection, actor, root, source["revision_id"])["state"] != "complete":
        pass
    with tenant_transaction(connection, actor):
        chunk = connection.execute(
            "SELECT id FROM evidence_chunks WHERE revision_id = %s LIMIT 1",
            (source["revision_id"],),
        ).fetchone()[0]
        claim = connection.execute(
            "INSERT INTO source_claims (organization_id, chunk_id, proposition, confidence) "
            "VALUES (%s, %s, %s, 1) RETURNING id", (actor.organization_id, chunk, content)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO claim_decisions (organization_id, claim_id, decision, decided_by, rationale) "
            "VALUES (%s, %s, 'confirmed', %s, 'fixture')",
            (actor.organization_id, claim, actor.actor.user_id),
        )
        obj = connection.execute(
            "INSERT INTO semantic_objects (organization_id, kind, stable_key) "
            "VALUES (%s, 'policy', %s) RETURNING id", (actor.organization_id, key)
        ).fetchone()[0]
        revision = connection.execute(
            "INSERT INTO semantic_revisions (organization_id, object_id, revision, rationale, "
            "content, valid_from) VALUES (%s, %s, 1, 'fixture', %s, %s) RETURNING id",
            (actor.organization_id, obj, '{"description":"approved policy"}',
             datetime(2020, 1, 1, tzinfo=UTC)),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO semantic_revision_claims (organization_id, revision_id, claim_id) "
            "VALUES (%s, %s, %s)", (actor.organization_id, revision, claim)
        )
        blueprint = connection.execute(
            "INSERT INTO blueprints (organization_id, blueprint_version, content_hash, "
            "status, confirmed_by) VALUES (%s, %s, %s, 'active', %s) RETURNING id",
            (actor.organization_id, version, "a" * 64, actor.actor.user_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO blueprint_members (organization_id, blueprint_id, revision_id) "
            "VALUES (%s, %s, %s)", (actor.organization_id, blueprint, revision)
        )
    return source, chunk, blueprint


def test_labeled_recall_versions_tenants_and_unknown(database, tmp_path):
    first, first_case = organization(database, "first")
    second, second_case = organization(database, "second")
    _source, first_chunk, first_blueprint = approved_source(
        database, first, first_case, tmp_path, "returns.txt",
        "Partial receipt of supplier orders leaves the undelivered balance open.")
    _other, second_chunk, _second_blueprint = approved_source(
        database, second, second_case, tmp_path, "returns.txt",
        "Partial receipt of supplier orders closes the undelivered balance.")
    queries = [
        ("paraphrase", "How is a partially received supplier order handled?", {first_chunk}),
        ("exact_id", "What does returns_policy say?", set()),
        ("cross_tenant_lookalike", "partial receipt supplier orders", {first_chunk}),
        ("absent_answer", "What is the office holiday menu?", set()),
    ]
    relevant_found = 0
    relevant_total = 0
    latencies = []
    for label, question, expected in queries:
        packet = retrieve(database, first, question)
        selected = set(packet["citations"])
        assert second_chunk not in selected, label
        if label == "absent_answer":
            assert packet["status"] == "unknown"
        elif label == "exact_id":
            assert packet["exact"] and packet["blueprint_id"] == first_blueprint
        else:
            relevant_found += len(selected & expected)
            relevant_total += len(expected)
        latencies.append(packet["latency_ms"])
    assert relevant_found / relevant_total >= 0.95
    assert median(latencies) >= 0
    p95 = quantiles(latencies, n=20, method="inclusive")[18]
    assert p95 >= median(latencies)
    logger.info("retrieval evaluation recall=%s p50_ms=%.2f p95_ms=%.2f",
                relevant_found / relevant_total, median(latencies), p95)


def test_wrong_version_and_department_scope(database, tmp_path):
    actor, case = organization(database, "versioned")
    source, old_chunk, old_blueprint = approved_source(
        database, actor, case, tmp_path, "old.txt", "Warranty approvals require owner review.")
    with tenant_transaction(database, actor):
        database.execute("UPDATE blueprints SET status = 'superseded' WHERE id = %s", (old_blueprint,))
    _new, new_chunk, new_blueprint = approved_source(
        database, actor, case, tmp_path, "new.txt", "Warranty approvals require manager review.",
        version=2, key="warranty_new")
    current = retrieve(database, actor, "warranty approvals review")
    historical = retrieve(database, actor, "warranty approvals review", blueprint_id=old_blueprint)
    assert current["blueprint_id"] == new_blueprint
    assert new_chunk in current["citations"] and old_chunk not in current["citations"]
    assert old_chunk in historical["citations"] and new_chunk not in historical["citations"]
    department = database.execute(
        "INSERT INTO departments (organization_id, slug, name) VALUES (%s, 'sales', 'Sales') "
        "RETURNING id", (actor.organization_id,)
    ).fetchone()[0]
    employee_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'employee', 'employee', 'test', false) RETURNING id",
        (actor.organization_id,),
    ).fetchone()[0]
    grant = DepartmentGrant(department, frozenset({"evidence.read", "policies.read"}))
    employee = TenantContext.from_auth(AuthContext(
        employee_id, actor.organization_id, "employee", True, True, grants=(grant,)))
    with tenant_transaction(database, actor):
        database.execute(
            "UPDATE evidence_sources SET access_label = 'owner', owner_user_id = %s "
            "WHERE id = %s", (actor.actor.user_id, source["source_id"])
        )
    assert retrieve(database, employee, "warranty approvals review", department_id=department)[
        "status"] == "supported"
    assert retrieve(database, employee, "warranty approvals review",
                    department_id=department, blueprint_id=old_blueprint)["status"] == "unknown"
    with tenant_transaction(database, employee):
        assert database.execute(
            "SELECT count(*) FROM semantic_revisions sr JOIN blueprint_members bm "
            "ON bm.revision_id = sr.id WHERE bm.blueprint_id = %s", (old_blueprint,)
        ).fetchone()[0] == 0
        assert database.execute(
            "SELECT count(*) FROM source_claims c WHERE c.chunk_id = %s", (old_chunk,)
        ).fetchone()[0] == 0
    with pytest.raises(PermissionError):
        retrieve(database, employee, "warranty approvals review")


def test_conflicting_sources_and_sql_total(database, tmp_path):
    actor, case = organization(database, "conflicting")
    _first, first_chunk, blueprint = approved_source(
        database, actor, case, tmp_path, "first.txt", "Returns require manager review.")
    _second, second_chunk, second_blueprint = approved_source(
        database, actor, case, tmp_path, "second.txt", "Returns need no manager review.",
        version=2, key="returns_exception")
    with tenant_transaction(database, actor):
        revision = database.execute(
            "SELECT revision_id FROM blueprint_members WHERE blueprint_id = %s",
            (second_blueprint,),
        ).fetchone()[0]
        database.execute(
            "INSERT INTO blueprint_members (organization_id, blueprint_id, revision_id) "
            "VALUES (%s, %s, %s)", (actor.organization_id, blueprint, revision)
        )
        first_claim = database.execute(
            "SELECT id FROM source_claims WHERE chunk_id = %s", (first_chunk,)
        ).fetchone()[0]
        second_claim = database.execute(
            "SELECT id FROM source_claims WHERE chunk_id = %s", (second_chunk,)
        ).fetchone()[0]
        low, high = sorted((first_claim, second_claim))
        database.execute(
            "INSERT INTO claim_conflicts (organization_id, first_claim_id, second_claim_id, reason) "
            "VALUES (%s, %s, %s, 'contradiction')", (actor.organization_id, low, high)
        )
        database.execute("UPDATE blueprints SET status = 'superseded' WHERE id = %s",
                         (second_blueprint,))
    database.execute(
        "INSERT INTO business_objects (organization_id, kind, external_id, source_path) "
        "VALUES (%s, 'record', 'SO-101', 'records/so-101.json')", (actor.organization_id,)
    )
    conflict = retrieve(database, actor, "returns manager review", blueprint_id=blueprint)
    assert conflict["status"] == "unknown" and conflict["conflict_count"] == 1
    total = retrieve(database, actor, "How many active records?", blueprint_id=second_blueprint)
    assert total["status"] == "unknown"
    current_total = retrieve(database, actor, "How many active records?")
    assert current_total["aggregate"]["value"] == 1
    assert current_total["aggregate"]["method"] == "sql_count"


class FakeMessages:
    def __init__(self, answer):
        self.answer = answer
        self.counts = 0
        self.messages = self

    def count_tokens(self, **kwargs):
        self.counts += 1
        return type("Count", (), {"input_tokens": len(str(kwargs["messages"])) // 3})()

    def create(self, **_kwargs):
        block = type("Block", (), {"type": "text", "text": self.answer})()
        usage = type("Usage", (), {"output_tokens": 42})()
        return type("Response", (), {"stop_reason": "end_turn", "content": [block],
                                     "usage": usage})()


def test_citation_validity_numeric_claim_and_budget():
    citation = uuid4()
    packet = {"status": "supported", "coverage": True, "blueprint_id": uuid4(),
              "exact": [], "semantic": [], "passages": [
                  {"id": citation, "text": "The policy limit is 100 USD."}]}
    valid = FakeMessages(f"The limit is 100 USD [{citation}].")
    result = answer_packet(valid, "What is the limit?", packet,
                           input_usd_per_million=3, output_usd_per_million=15)
    assert result["status"] == "supported" and valid.counts == 1
    assert result["input_tokens"] <= INPUT_TARGET < INPUT_HARD_CAP
    assert result["estimated_cost_usd"] > 0
    invalid = FakeMessages(f"The limit is 999 USD [{citation}].")
    assert answer_packet(invalid, "What is the limit?", packet)["status"] == "unknown"
    uncited = FakeMessages("The limit is 100 USD.")
    assert answer_packet(uncited, "What is the limit?", packet)["status"] == "unknown"
