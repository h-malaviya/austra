"""Tenant scoped review cases and operational measurements."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from austra.onboarding.evidence import EMBEDDING_MODEL
from austra.tenant.context import TenantContext, tenant_transaction

EXPIRY_NOTICE = timedelta(days=7)
METRICS = frozenset(
    {
        "retrieval_miss",
        "citation_failure",
        "token_budget_breach",
        "task_resolved",
        "task_cost_usd",
        "task_latency_ms",
        "connection_failure",
        "release_defect",
        "repair_time_ms",
    }
)
CITATION_TABLES = {
    "claim_id": "source_claims",
    "semantic_revision_id": "semantic_revisions",
    "runtime_event_id": "runtime_events",
    "build_id": "builds",
    "release_id": "releases",
    "source_id": "evidence_sources",
    "evidence_revision_id": "evidence_revisions",
    "blueprint_id": "blueprints",
}


def open_review(
    connection: psycopg.Connection,
    context: TenantContext,
    *,
    trigger: str,
    subject_id: UUID,
    owner_user_id: UUID,
    citations: list[dict],
    impact: str,
    next_step: str,
    blueprint_id: UUID | None = None,
) -> UUID:
    if not citations or not impact.strip() or not next_step.strip():
        raise ValueError("Review requires sources, impact, and a proposed next step")
    row = connection.execute(
        "INSERT INTO review_cases (organization_id, trigger_kind, subject_id, "
        "owner_user_id, blueprint_id, citations, impact, next_step) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (organization_id, trigger_kind, subject_id) WHERE state = 'open' "
        "DO UPDATE SET citations = EXCLUDED.citations, impact = EXCLUDED.impact, "
        "next_step = EXCLUDED.next_step RETURNING id",
        (
            context.organization_id,
            trigger,
            subject_id,
            owner_user_id,
            blueprint_id,
            Jsonb(citations),
            impact,
            next_step,
        ),
    ).fetchone()
    return row[0]


def record_metric(
    connection: psycopg.Connection,
    context: TenantContext,
    metric: str,
    value: Decimal | float = 1,
    task_id: UUID | None = None,
) -> None:
    if metric not in METRICS or not Decimal(str(value)).is_finite() or value < 0:
        raise ValueError("Invalid operational metric")
    with tenant_transaction(connection, context):
        connection.execute(
            "INSERT INTO operational_metrics (organization_id, task_id, metric, value) "
            "VALUES (%s, %s, %s, %s)",
            (context.organization_id, task_id, metric, value),
        )


def scan_policy_expiry(
    connection: psycopg.Connection, context: TenantContext, at: datetime | None = None
) -> list[UUID]:
    context.require("policies.read")
    at = at or datetime.now(UTC)
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT r.id, r.owner_user_id, r.valid_to, b.id, b.confirmed_by, "
            "array_remove(array_agg(rc.claim_id), NULL) FROM releases l "
            "JOIN blueprints b ON b.id = l.blueprint_id AND b.organization_id = l.organization_id "
            "JOIN blueprint_members bm ON bm.blueprint_id = b.id AND bm.organization_id = b.organization_id "
            "JOIN semantic_revisions r ON r.id = bm.revision_id AND r.organization_id = b.organization_id "
            "JOIN semantic_objects o ON o.id = r.object_id AND o.organization_id = b.organization_id "
            "LEFT JOIN semantic_revision_claims rc ON rc.revision_id = r.id "
            "AND rc.organization_id = b.organization_id "
            "WHERE l.organization_id = %s AND l.state = 'active' "
            "AND o.kind IN ('policy', 'authority') AND r.valid_to IS NOT NULL "
            "AND r.valid_to <= %s GROUP BY r.id, b.id",
            (context.organization_id, at + EXPIRY_NOTICE),
        ).fetchall()
        return [
            open_review(
                connection,
                context,
                trigger="policy_expiry",
                subject_id=revision_id,
                owner_user_id=owner or approver,
                blueprint_id=blueprint_id,
                citations=[{"semantic_revision_id": str(revision_id)}]
                + [{"claim_id": str(claim)} for claim in claims],
                impact=f"Active rule expires at {expires.isoformat()}; affected actions must stop afterward.",
                next_step="Clarify and confirm a successor rule, then build and preview the next blueprint.",
            )
            for revision_id, owner, expires, blueprint_id, approver, claims in rows
        ]


def scan_command_expiry(
    connection: psycopg.Connection,
    context: TenantContext,
    action: str,
    at: datetime,
    transition_key: str | None = None,
    capability_key: str | None = None,
) -> None:
    """Open an owner's review before an action reaches the edge of its dated rule."""
    with tenant_transaction(connection, context):
        releases = connection.execute(
            "SELECT r.blueprint_id, r.approved_by, a.manifest FROM releases r "
            "JOIN capability_build_artifacts a ON a.build_id = r.build_id "
            "AND a.organization_id = r.organization_id "
            "WHERE r.organization_id = %s AND r.state = 'active'",
            (context.organization_id,),
        ).fetchall()
        key = (
            f"command.{capability_key}.{transition_key}"
            if action == "transition_record"
            else f"command.{action}"
        )
        for release in releases:
            for rule in release[2].get("decision_tables", []):
                if rule.get("content", {}).get("decision_key") != key or not rule.get("valid_to"):
                    continue
                expires = datetime.fromisoformat(rule["valid_to"])
                if expires > at + EXPIRY_NOTICE:
                    continue
                revision_id = UUID(str(rule["id"]))
                open_review(
                    connection,
                    context,
                    trigger="policy_expiry",
                    subject_id=revision_id,
                    owner_user_id=release[1],
                    blueprint_id=release[0],
                    citations=[
                        {"semantic_revision_id": str(revision_id), "blueprint_id": str(release[0])}
                    ],
                    impact=f"The rule for {key} expires at {expires.isoformat()}.",
                    next_step="Confirm a successor rule and release a new blueprint before expiry.",
                )


def report_outcome_conflict(
    connection: psycopg.Connection,
    context: TenantContext,
    event_id: UUID,
    expected: str,
    observed: str,
) -> UUID:
    context.require("workflows.approve")
    if not expected.strip() or not observed.strip():
        raise ValueError("Expected and observed outcomes are required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT e.payload, r.blueprint_id, r.approved_by FROM runtime_events e "
            "JOIN releases r ON r.organization_id = e.organization_id "
            "AND r.build_id = (e.payload->>'build_id')::uuid "
            "WHERE e.organization_id = %s AND e.id = %s",
            (context.organization_id, event_id),
        ).fetchone()
        if row is None:
            raise LookupError("Pinned runtime event not found")
        return open_review(
            connection,
            context,
            trigger="outcome_conflict",
            subject_id=event_id,
            owner_user_id=row[2],
            blueprint_id=row[1],
            citations=[
                {
                    "runtime_event_id": str(event_id),
                    "blueprint_id": str(row[1]),
                    "build_id": row[0]["build_id"],
                }
            ],
            impact=f"Expected: {expected.strip()}; observed: {observed.strip()}",
            next_step="Triage implementation behavior against the approved blueprint; clarify changed meaning.",
        )


def triage_feedback(
    connection: psycopg.Connection,
    context: TenantContext,
    release_id: UUID,
    classification: str,
    expected: str,
    observed: str,
    citations: list[dict],
) -> dict:
    context.require("workflows.approve")
    if classification not in {"implementation_bug", "business_meaning_change"}:
        raise ValueError("Classify feedback as implementation bug or business meaning change")
    if not expected.strip() or not observed.strip() or not citations:
        raise ValueError("Feedback requires expected, observed, and source citations")
    with tenant_transaction(connection, context):
        release = connection.execute(
            "SELECT blueprint_id, build_id, approved_by, workflow_case_id FROM releases "
            "WHERE organization_id = %s AND id = %s AND state = 'active' FOR UPDATE",
            (context.organization_id, release_id),
        ).fetchone()
        if release is None:
            raise LookupError("Active release not found")
        blueprint_id, build_id, owner, lineage = release
        for citation in citations:
            if not isinstance(citation, dict) or not set(citation) & CITATION_TABLES.keys():
                raise ValueError("Feedback citation must name a stored source or revision")
            for key, table in CITATION_TABLES.items():
                if key not in citation:
                    continue
                try:
                    cited_id = UUID(str(citation[key]))
                except (TypeError, ValueError) as error:
                    raise ValueError("Invalid feedback citation identifier") from error
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE organization_id = %s AND id = %s",
                        (context.organization_id, cited_id),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Feedback citation is outside this organization")
        if classification == "implementation_bug":
            existing = connection.execute(
                "SELECT id, patch_build_id FROM defects WHERE organization_id = %s "
                "AND build_id = %s AND expected = %s AND observed = %s "
                "AND state = 'open' AND patch_build_id IS NOT NULL LIMIT 1",
                (context.organization_id, build_id, expected, observed),
            ).fetchone()
            if existing:
                return {
                    "classification": classification,
                    "defect_id": existing[0],
                    "patch_build_id": existing[1],
                    "blueprint_id": blueprint_id,
                }
            connection.execute(
                "INSERT INTO operational_metrics (organization_id, metric, value) "
                "VALUES (%s, 'release_defect', 1)",
                (context.organization_id,),
            )
            defect_id = connection.execute(
                "INSERT INTO defects (organization_id, build_id, expected, observed, "
                "owner_user_id, trace) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    context.organization_id,
                    build_id,
                    expected,
                    observed,
                    owner,
                    Jsonb({"classification": classification, "citations": citations}),
                ),
            ).fetchone()[0]
            revision = connection.execute(
                "SELECT coalesce(max(build_revision), 0) + 1 FROM builds "
                "WHERE organization_id = %s AND blueprint_id = %s",
                (context.organization_id, blueprint_id),
            ).fetchone()[0]
            patch_id = connection.execute(
                "INSERT INTO builds (organization_id, blueprint_id, build_revision) "
                "VALUES (%s, %s, %s) RETURNING id",
                (context.organization_id, blueprint_id, revision),
            ).fetchone()[0]
            connection.execute(
                "UPDATE defects SET patch_build_id = %s WHERE organization_id = %s AND id = %s",
                (patch_id, context.organization_id, defect_id),
            )
            open_review(
                connection,
                context,
                trigger="failed_run",
                subject_id=defect_id,
                owner_user_id=owner,
                blueprint_id=blueprint_id,
                citations=citations + [{"release_id": str(release_id), "build_id": str(build_id)}],
                impact=f"Expected: {expected}; observed: {observed}",
                next_step="Fix and independently verify the patch build under the same blueprint.",
            )
            return {
                "classification": classification,
                "defect_id": defect_id,
                "patch_build_id": patch_id,
                "blueprint_id": blueprint_id,
            }
        review_id = open_review(
            connection,
            context,
            trigger="business_meaning_change",
            subject_id=release_id,
            owner_user_id=owner,
            blueprint_id=blueprint_id,
            citations=citations
            + [{"release_id": str(release_id), "blueprint_id": str(blueprint_id)}],
            impact=f"Expected: {expected}; observed: {observed}",
            next_step="Clarify, confirm the next blueprint, build, verify, and preview before release.",
        )
        case = connection.execute(
            "SELECT id FROM onboarding_cases WHERE organization_id = %s "
            "AND state <> 'locked_candidate' AND workflow_root_id IS NOT DISTINCT FROM %s "
            "AND (%s::uuid IS NOT NULL OR (parent_case_id IS NULL AND workflow_name IS NULL))",
            (context.organization_id, lineage, lineage),
        ).fetchone()
        if case is None and lineage:
            case = connection.execute(
                "INSERT INTO onboarding_cases (organization_id, created_event_id, state, parent_case_id, "
                "workflow_root_id, workflow_name) SELECT organization_id, %s, 'clarifying', "
                "coalesce(parent_case_id, id), id, workflow_name FROM onboarding_cases "
                "WHERE organization_id = %s AND id = %s RETURNING id",
                (review_id, context.organization_id, lineage),
            ).fetchone()
        if case is None:
            case = connection.execute(
                "INSERT INTO onboarding_cases (organization_id, created_event_id, state) "
                "VALUES (%s, %s, 'clarifying') RETURNING id",
                (context.organization_id, review_id),
            ).fetchone()
        return {
            "classification": classification,
            "review_case_id": review_id,
            "clarification_case_id": case[0],
            "active_blueprint_id": blueprint_id,
        }


def review_dashboard(connection: psycopg.Connection, context: TenantContext) -> dict:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        metrics = dict(
            connection.execute(
                "SELECT metric, sum(value) FROM operational_metrics WHERE organization_id = %s "
                "GROUP BY metric",
                (context.organization_id,),
            ).fetchall()
        )
        resolved = metrics.get("task_resolved", 0)
        connections = dict(
            connection.execute(
                "SELECT health, count(*) FROM integration_connections WHERE organization_id = %s "
                "GROUP BY health",
                (context.organization_id,),
            ).fetchall()
        )
        freshness = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE c.embedding IS NOT NULL "
            "AND c.embedding_model = %s) FROM evidence_chunks c "
            "JOIN evidence_revisions r ON r.id = c.revision_id "
            "WHERE c.organization_id = %s AND r.status = 'complete' "
            "AND r.revision = (SELECT max(newer.revision) FROM evidence_revisions newer "
            "WHERE newer.organization_id = r.organization_id AND newer.source_id = r.source_id)",
            (EMBEDDING_MODEL, context.organization_id),
        ).fetchone()
        sources = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE latest.status <> 'complete'), "
            "max(latest.captured_at) FROM evidence_sources s "
            "JOIN LATERAL (SELECT status, captured_at FROM evidence_revisions r "
            "WHERE r.organization_id = s.organization_id AND r.source_id = s.id "
            "ORDER BY revision DESC LIMIT 1) latest ON true "
            "WHERE s.organization_id = %s AND s.excluded = false",
            (context.organization_id,),
        ).fetchone()
        open_cases = connection.execute(
            "SELECT count(*) FROM review_cases WHERE organization_id = %s AND state = 'open'",
            (context.organization_id,),
        ).fetchone()[0]
        defects = connection.execute(
            "SELECT count(*) FROM defects d JOIN releases r ON r.build_id = d.build_id "
            "WHERE d.organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        releases = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE state = 'rolled_back') "
            "FROM releases WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()
        latency = connection.execute(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY value), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY value) "
            "FROM operational_metrics WHERE organization_id = %s "
            "AND metric = 'task_latency_ms'",
            (context.organization_id,),
        ).fetchone()
        repair = connection.execute(
            "SELECT avg(value) FROM operational_metrics WHERE organization_id = %s "
            "AND metric = 'repair_time_ms'",
            (context.organization_id,),
        ).fetchone()[0]
        automatic_email = dict(connection.execute(
            "SELECT state,count(*) FROM workflow_automatic_replies WHERE organization_id=%s GROUP BY state",
            (context.organization_id,),
        ).fetchall())
    return {
        "metrics": metrics,
        "cost_per_resolved_task_usd": metrics["task_cost_usd"] / resolved
        if resolved and "task_cost_usd" in metrics
        else None,
        "connection_health": connections,
        "automatic_email": automatic_email,
        "embedding_freshness": {
            "current_chunks": freshness[0],
            "current_model_chunks": freshness[1],
        },
        "source_freshness": {
            "sources": sources[0],
            "pending_or_failed": sources[1],
            "latest_capture_at": sources[2],
        },
        "open_review_cases": open_cases,
        "released_build_defects": defects,
        "defect_escape_rate": defects / releases[0] if releases[0] else None,
        "rollback_signals": releases[1],
        "latency_ms": {"p50": latency[0], "p95": latency[1]},
        "mean_repair_time_ms": repair,
    }


def resolve_review(connection: psycopg.Connection, context: TenantContext, case_id: UUID) -> None:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "UPDATE review_cases SET state = 'resolved', resolved_at = now() "
            "WHERE organization_id = %s AND id = %s AND state = 'open' "
            "RETURNING opened_at",
            (context.organization_id, case_id),
        ).fetchone()
        if row is None:
            raise LookupError("Open review case not found")
        duration_ms = max(0, int((datetime.now(UTC) - row[0]).total_seconds() * 1000))
        connection.execute(
            "INSERT INTO operational_metrics (organization_id, metric, value) "
            "VALUES (%s, 'repair_time_ms', %s)",
            (context.organization_id, duration_ms),
        )


def list_reviews(connection: psycopg.Connection, context: TenantContext) -> list[dict]:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT id, trigger_kind, subject_id, owner_user_id, citations, impact, "
            "next_step, blueprint_id, opened_at FROM review_cases WHERE organization_id = %s "
            "AND state = 'open' ORDER BY opened_at DESC",
            (context.organization_id,),
        ).fetchall()
    return [
        dict(
            zip(
                (
                    "id",
                    "trigger",
                    "subject_id",
                    "owner_user_id",
                    "citations",
                    "impact",
                    "next_step",
                    "blueprint_id",
                    "opened_at",
                ),
                row,
            )
        )
        for row in rows
    ]
