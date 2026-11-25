"""Immutable semantic drafts and a strict blueprint compiler."""

import hashlib
import json
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from austra.connectors import GMAIL_SCOPE
from austra.onboarding.decisions import unique_decisions
from austra.onboarding.evidence import EMBEDDING_MODEL, _embedding
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.keys import ADMIN_ONLY_PERMISSIONS, DEPARTMENT_PERMISSIONS

KINDS = frozenset(
    {
        "capability",
        "actor",
        "authority",
        "policy",
        "knowledge",
        "event",
        "workflow",
        "business_object",
        "integration",
        "projection",
    }
)
RELATIONS = {
    "authorizes": ("authority", "actor"),
    "governs": ("policy", "capability"),
    "supports": ("knowledge", "policy"),
    "triggers": ("event", "workflow"),
    "uses": ("capability", "integration"),
    "reads": ("workflow", "business_object"),
    "writes": ("workflow", "business_object"),
    "projects": ("projection", "capability"),
    "owned_by": ("capability", "actor"),
    "invokes": ("workflow", "capability"),
    "constrains": ("authority", "capability"),
}
QUESTION_ORDER = ("authority", "side_effect", "source_of_truth", "units", "exception", "owner")
SUPPORTED_ACTION_SCOPES = {
    "gmail.send_email": set(GMAIL_SCOPE.split()),
    "slack.post_message": {"chat:write"},
    "github.list_repositories": {"repo"},
    "github.create_issue": {"repo"},
}


def _admin(context: TenantContext, permission: str = "policies.draft") -> None:
    context.require(permission)
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def extract_claims(
    connection: psycopg.Connection, context: TenantContext, chunk_id: UUID
) -> list[dict]:
    """Offer bounded, cited sentences as unconfirmed claims; never infer policy status."""
    _admin(context)
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT c.text FROM evidence_chunks c JOIN evidence_revisions r ON r.id = c.revision_id "
            "JOIN evidence_sources s ON s.id = r.source_id WHERE c.organization_id = %s "
            "AND c.id = %s AND r.status = 'complete' AND NOT s.excluded "
            "AND s.review_state = 'included'",
            (context.organization_id, chunk_id),
        ).fetchone()
        if row is None:
            raise LookupError("Eligible source passage not found")
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", row[0]) if part.strip()]
        result = []
        for sentence in sentences[:20]:
            claim_id = connection.execute(
                "INSERT INTO source_claims (organization_id, chunk_id, proposition, confidence) "
                "VALUES (%s, %s, %s, 0) RETURNING id",
                (context.organization_id, chunk_id, sentence),
            ).fetchone()[0]
            result.append({"id": claim_id, "proposition": sentence, "status": "unconfirmed"})
        return result


def decide_claim(
    connection: psycopg.Connection,
    context: TenantContext,
    claim_id: UUID,
    decision: str,
    rationale: str,
) -> UUID:
    _admin(context, "policies.approve")
    if decision not in {"confirmed", "rejected", "superseded"} or not rationale.strip():
        raise ValueError("A claim decision and rationale are required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT id FROM source_claims WHERE organization_id = %s AND id = %s",
            (context.organization_id, claim_id),
        ).fetchone()
        if row is None:
            raise LookupError("Claim not found")
        return connection.execute(
            "INSERT INTO claim_decisions (organization_id, claim_id, decision, decided_by, "
            "rationale) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (context.organization_id, claim_id, decision, context.actor.user_id, rationale.strip()),
        ).fetchone()[0]


def record_conflict(
    connection: psycopg.Connection,
    context: TenantContext,
    first_claim_id: UUID,
    second_claim_id: UUID,
    reason: str,
) -> None:
    _admin(context)
    if first_claim_id == second_claim_id or not reason.strip():
        raise ValueError("Two different claims and a reason are required")
    first, second = sorted((first_claim_id, second_claim_id))
    with tenant_transaction(connection, context):
        claims = connection.execute(
            "SELECT id FROM source_claims WHERE organization_id = %s AND id = ANY(%s)",
            (context.organization_id, [first, second]),
        ).fetchall()
        if len(claims) != 2:
            raise LookupError("Claims not found in this organization")
        connection.execute(
            "INSERT INTO claim_conflicts (organization_id, first_claim_id, second_claim_id, reason) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (context.organization_id, first, second, reason.strip()),
        )


def add_revision(
    connection: psycopg.Connection,
    context: TenantContext,
    *,
    kind: str,
    stable_key: str,
    content: dict,
    rationale: str,
    claim_ids: list[UUID],
    owner_user_id: UUID | None,
    valid_from: datetime,
    valid_to: datetime | None = None,
) -> dict:
    _admin(context)
    if kind not in KINDS or not re.fullmatch(r"[a-z][a-z0-9_]*", stable_key):
        raise ValueError("Invalid semantic kind or key")
    if not isinstance(content, dict) or not rationale.strip() or not claim_ids:
        raise ValueError("Content, rationale, and source claims are required")
    if valid_to is not None and valid_to <= valid_from:
        raise ValueError("Invalid valid-time interval")
    with tenant_transaction(connection, context):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{context.organization_id}:{kind}:{stable_key}",),
        )
        if (
            owner_user_id is not None
            and connection.execute(
                "SELECT semantic_owner_active(%s)",
                (owner_user_id,),
            ).fetchone()[0]
            is False
        ):
            raise ValueError("Owner is not an active organization user")
        claims = connection.execute(
            "SELECT c.id, s.kind, coalesce((SELECT d.decision FROM claim_decisions d "
            "WHERE d.organization_id = c.organization_id AND d.claim_id = c.id "
            "ORDER BY d.decision_order DESC LIMIT 1), 'unconfirmed') "
            "FROM source_claims c JOIN evidence_chunks e ON e.id = c.chunk_id "
            "JOIN evidence_revisions r ON r.id = e.revision_id "
            "JOIN evidence_sources s ON s.id = r.source_id "
            "WHERE c.organization_id = %s AND c.id = ANY(%s) AND NOT s.excluded "
            "AND s.review_state = 'included'",
            (context.organization_id, claim_ids),
        ).fetchall()
        if len(set(claim_ids)) != len(claims):
            raise ValueError("A source claim is missing or excluded")
        if kind in {"policy", "authority", "workflow"} and all(
            source_kind in {"website", "linkedin_export", "linkedin_manual"}
            for _, source_kind, _ in claims
        ):
            raise ValueError("Public or marketing claims alone cannot support an operating rule")
        object_row = connection.execute(
            "SELECT id FROM semantic_objects WHERE organization_id = %s AND kind = %s "
            "AND stable_key = %s",
            (context.organization_id, kind, stable_key),
        ).fetchone()
        object_id = (
            object_row[0]
            if object_row
            else connection.execute(
                "INSERT INTO semantic_objects (organization_id, kind, stable_key) "
                "VALUES (%s, %s, %s) RETURNING id",
                (context.organization_id, kind, stable_key),
            ).fetchone()[0]
        )
        previous = connection.execute(
            "SELECT id, revision FROM semantic_revisions WHERE organization_id = %s "
            "AND object_id = %s ORDER BY revision DESC LIMIT 1",
            (context.organization_id, object_id),
        ).fetchone()
        number = previous[1] + 1 if previous else 1
        revision_id = connection.execute(
            "INSERT INTO semantic_revisions (organization_id, object_id, revision, owner_user_id, "
            "rationale, content, valid_from, valid_to, supersedes_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                object_id,
                number,
                owner_user_id,
                rationale.strip(),
                Jsonb(content),
                valid_from,
                valid_to,
                previous[0] if previous else None,
            ),
        ).fetchone()[0]
        for claim_id in set(claim_ids):
            connection.execute(
                "INSERT INTO semantic_revision_claims (organization_id, revision_id, claim_id) "
                "VALUES (%s, %s, %s)",
                (context.organization_id, revision_id, claim_id),
            )
        return {"id": revision_id, "object_id": object_id, "revision": number}


def add_edge(
    connection: psycopg.Connection,
    context: TenantContext,
    from_revision_id: UUID,
    relation: str,
    to_revision_id: UUID,
    rationale: str,
) -> UUID:
    _admin(context)
    if relation not in RELATIONS or not rationale.strip():
        raise ValueError("Invalid relationship")
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT r.id, o.kind FROM semantic_revisions r JOIN semantic_objects o ON o.id = r.object_id "
            "WHERE r.organization_id = %s AND r.id = ANY(%s)",
            (context.organization_id, [from_revision_id, to_revision_id]),
        ).fetchall()
        kinds = dict(rows)
        if (kinds.get(from_revision_id), kinds.get(to_revision_id)) != RELATIONS[relation]:
            raise ValueError("Relationship endpoints do not match their types")
        return connection.execute(
            "INSERT INTO semantic_edges (organization_id, from_revision_id, relation, "
            "to_revision_id, rationale) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                from_revision_id,
                relation,
                to_revision_id,
                rationale.strip(),
            ),
        ).fetchone()[0]


def _load_graph(
    connection: psycopg.Connection,
    organization_id: UUID,
    revision_ids: list[UUID],
    edge_ids: list[UUID],
) -> tuple[list[dict], list[dict]]:
    rows = connection.execute(
        "SELECT r.id, o.kind, o.stable_key, r.owner_user_id, r.rationale, r.content, "
        "r.valid_from, r.valid_to, r.recorded_at FROM semantic_revisions r "
        "JOIN semantic_objects o ON o.id = r.object_id WHERE r.organization_id = %s "
        "AND r.id = ANY(%s)",
        (organization_id, revision_ids),
    ).fetchall()
    revisions = [
        dict(
            zip(
                (
                    "id",
                    "kind",
                    "key",
                    "owner",
                    "rationale",
                    "content",
                    "valid_from",
                    "valid_to",
                    "recorded_at",
                ),
                row,
            )
        )
        for row in rows
    ]
    links = connection.execute(
        "SELECT id, from_revision_id, relation, to_revision_id, rationale FROM semantic_edges "
        "WHERE organization_id = %s AND id = ANY(%s)",
        (organization_id, edge_ids),
    ).fetchall()
    edges = [dict(zip(("id", "from", "relation", "to", "rationale"), row)) for row in links]
    return revisions, edges


def _overlap(first: dict, second: dict) -> bool:
    return (first["valid_to"] is None or second["valid_from"] < first["valid_to"]) and (
        second["valid_to"] is None or first["valid_from"] < second["valid_to"]
    )


def _checks(
    connection: psycopg.Connection, organization_id: UUID, proposal: dict
) -> tuple[list[dict], dict]:
    revision_ids = [UUID(str(value)) for value in proposal.get("revision_ids", [])]
    edge_ids = [UUID(str(value)) for value in proposal.get("edge_ids", [])]
    issues = []
    if len(set(revision_ids)) != len(revision_ids) or len(set(edge_ids)) != len(edge_ids):
        issues.append({"type": "broken_link", "detail": "Duplicate graph member"})
    revisions, edges = _load_graph(connection, organization_id, revision_ids, edge_ids)
    if (
        not revisions
        or len(revisions) != len(set(revision_ids))
        or len(edges) != len(set(edge_ids))
    ):
        issues.append({"type": "broken_link", "detail": "Missing tenant graph member"})
    by_id = {item["id"]: item for item in revisions}
    for edge in edges:
        if edge["from"] not in by_id or edge["to"] not in by_id:
            issues.append({"type": "broken_link", "detail": str(edge["id"])})
    claim_rows = connection.execute(
        "SELECT rc.revision_id, c.id, s.kind, s.display_name, e.locator, r.captured_at, "
        "(s.excluded OR s.review_state <> 'included'), "
        "coalesce((SELECT d.decision FROM claim_decisions d WHERE "
        "d.organization_id = c.organization_id AND d.claim_id = c.id "
        "ORDER BY d.decision_order DESC LIMIT 1), 'unconfirmed') "
        "FROM semantic_revision_claims rc JOIN source_claims c ON c.id = rc.claim_id "
        "JOIN evidence_chunks e ON e.id = c.chunk_id "
        "JOIN evidence_revisions r ON r.id = e.revision_id "
        "JOIN evidence_sources s ON s.id = r.source_id "
        "WHERE rc.organization_id = %s AND rc.revision_id = ANY(%s)",
        (organization_id, revision_ids),
    ).fetchall()
    sources: dict[UUID, list[dict]] = {item["id"]: [] for item in revisions}
    for (
        revision_id,
        claim_id,
        source_kind,
        name,
        locator,
        captured_at,
        excluded,
        decision,
    ) in claim_rows:
        sources[revision_id].append(
            {
                "claim_id": claim_id,
                "source_kind": source_kind,
                "name": name,
                "locator": locator,
                "captured_at": captured_at,
                "decision": decision,
            }
        )
        if excluded or decision != "confirmed":
            issues.append({"type": "unconfirmed_claim", "detail": str(claim_id)})
    for item in revisions:
        content = item["content"]
        if (
            not item["owner"]
            or connection.execute(
                "SELECT semantic_owner_active(%s)",
                (item["owner"],),
            ).fetchone()[0]
            is False
        ):
            issues.append({"type": "owner", "detail": item["key"]})
        if not sources[item["id"]]:
            issues.append({"type": "source", "detail": item["key"]})
        if (
            item["kind"] in {"policy", "authority", "workflow"}
            and sources[item["id"]]
            and all(
                source["source_kind"] in {"website", "linkedin_export", "linkedin_manual"}
                for source in sources[item["id"]]
            )
        ):
            issues.append({"type": "marketing_source", "detail": item["key"]})
        if item["kind"] in {"policy", "authority"}:
            for field in ("decision_key", "scope", "effect"):
                if not content.get(field):
                    issues.append({"type": "rule", "detail": f"{item['key']}: {field}"})
            if item["kind"] == "authority" and (
                not content.get("limit") or not content.get("unit")
            ):
                issues.append({"type": "units", "detail": item["key"]})
            if item["kind"] == "authority":
                permission = content.get("required_permission")
                authorized_actors = [
                    by_id[edge["to"]]
                    for edge in edges
                    if edge["from"] == item["id"]
                    and edge["relation"] == "authorizes"
                    and edge["to"] in by_id
                ]
                if (
                    not permission
                    or not authorized_actors
                    or any(
                        (
                            actor["content"].get("platform_role") == "org_admin"
                            and permission not in DEPARTMENT_PERMISSIONS | ADMIN_ONLY_PERMISSIONS
                        )
                        or (
                            actor["content"].get("platform_role") == "employee"
                            and permission not in DEPARTMENT_PERMISSIONS
                        )
                        or actor["content"].get("platform_role") not in {"org_admin", "employee"}
                        for actor in authorized_actors
                    )
                ):
                    issues.append({"type": "authority", "detail": item["key"]})
            required_relations = (
                {"authorizes", "constrains"} if item["kind"] == "authority" else {"governs"}
            )
            actual_relations = {edge["relation"] for edge in edges if edge["from"] == item["id"]}
            if not required_relations <= actual_relations:
                issues.append({"type": "authority", "detail": item["key"]})
        if item["kind"] == "business_object" and not content.get("source_of_truth"):
            issues.append({"type": "source_of_truth", "detail": item["key"]})
        if item["kind"] == "integration":
            requested = set(content.get("requested_scopes", []))
            action_ids = content.get("action_ids", [])
            supported = set().union(
                *(SUPPORTED_ACTION_SCOPES.get(action_id, set()) for action_id in action_ids)
            )
            generic = all(
                isinstance(action_id, str)
                and action_id.startswith("remote_mcp.")
                and len(action_id) > len("remote_mcp.")
                for action_id in action_ids
            )
            if (
                not requested
                or not action_ids
                or not requested <= supported
                or any(action_id not in SUPPORTED_ACTION_SCOPES for action_id in action_ids)
            ) and not (generic and requested):
                issues.append({"type": "integration_scope", "detail": item["key"]})
        if item["kind"] == "workflow":
            states = set(content.get("states", []))
            transitions = content.get("transitions", [])
            if (
                not states
                or not transitions
                or any(
                    transition.get("from") not in states or transition.get("to") not in states
                    for transition in transitions
                )
            ):
                issues.append({"type": "transition", "detail": item["key"]})
        if (
            item["kind"] == "capability"
            and content.get("external_actions")
            and not any(edge["relation"] == "uses" and edge["from"] == item["id"] for edge in edges)
        ):
            issues.append({"type": "integration_scope", "detail": item["key"]})
    rules = [item for item in revisions if item["kind"] in {"policy", "authority"}]
    for index, first in enumerate(rules):
        for second in rules[index + 1 :]:
            if (
                first["content"].get("decision_key") == second["content"].get("decision_key")
                and first["content"].get("scope") == second["content"].get("scope")
                and _overlap(first, second)
            ):
                issues.append(
                    {"type": "overlapping_rule", "detail": f"{first['key']} / {second['key']}"}
                )
    conflict_rows = connection.execute(
        "SELECT first_claim_id, second_claim_id FROM claim_conflicts WHERE organization_id = %s "
        "AND first_claim_id IN (SELECT claim_id FROM semantic_revision_claims WHERE "
        "organization_id = %s AND revision_id = ANY(%s)) AND second_claim_id IN "
        "(SELECT claim_id FROM semantic_revision_claims WHERE organization_id = %s "
        "AND revision_id = ANY(%s))",
        (organization_id, organization_id, revision_ids, organization_id, revision_ids),
    ).fetchall()
    for first, second in conflict_rows:
        issues.append({"type": "conflicting_claims", "detail": f"{first} / {second}"})
    behavior = proposal.get("behavior", {})
    if not isinstance(behavior, dict):
        issues.append({"type": "behavior", "detail": "summary"})
        behavior = {}
    for field in ("reads", "decisions", "actions", "approvers", "manual_steps", "outputs"):
        if field not in behavior or not isinstance(behavior[field], list):
            issues.append({"type": "behavior", "detail": field})
    if isinstance(behavior, dict):
        expected = {
            "reads": {item["key"] for item in revisions if item["kind"] == "business_object"},
            "decisions": {
                item["content"].get("decision_key")
                for item in revisions
                if item["kind"] == "authority"
            },
            "actions": {
                action
                for item in revisions
                if item["kind"] == "integration"
                for action in item["content"].get("action_ids", [])
            },
            "approvers": {item["key"] for item in revisions if item["kind"] == "actor"},
        }
        for field, allowed in expected.items():
            for value in behavior.get(field, []):
                if value not in allowed:
                    issues.append({"type": "broken_link", "detail": f"{field}: {value}"})
        if behavior.get("actions") and not any(item["kind"] == "authority" for item in revisions):
            issues.append({"type": "side_effect", "detail": "External action has no authority"})
    examples = proposal.get("acceptance_examples", [])
    for decision in proposal.get("open_decisions", []):
        if isinstance(decision, str) and decision.strip():
            issues.append({"type": "open_decision", "detail": decision.strip()})
    for setting in proposal.get("email_requirements", []):
        if not any(item["kind"] == "capability" and item["content"].get(setting) for item in revisions):
            issues.append({"type": "open_decision", "detail": f"Define the {setting.replace('_', ' ')} for review."})
    for category in ("normal", "boundary", "adverse"):
        if not any(
            example.get("category") == category and example.get("input") and example.get("expected")
            for example in examples
        ):
            issues.append({"type": "acceptance", "detail": category})
    manifest = {
        "decision_tables": [item for item in revisions if item["kind"] in {"policy", "authority"}],
        "event_routes": [item for item in revisions if item["kind"] in {"event", "workflow"}],
        "typed_mappings": [item for item in revisions if item["kind"] == "business_object"],
        "allowed_actions": [item for item in revisions if item["kind"] == "integration"],
        "projections": [item for item in revisions if item["kind"] == "projection"],
        "sources": sources,
        "edges": edges,
        "revisions": revisions,
    }
    return issues, manifest


def questions_for(issues: list[dict], manifest: dict) -> list[dict]:
    category = {
        "rule": "authority",
        "authority": "authority",
        "overlapping_rule": "authority",
        "conflicting_claims": "source_of_truth",
        "integration_scope": "side_effect",
        "units": "units",
        "source_of_truth": "source_of_truth",
        "owner": "owner",
        "transition": "exception",
        "unconfirmed_claim": "source_of_truth",
        "marketing_source": "source_of_truth",
    }
    questions = []
    for issue in issues:
        topic = category.get(issue["type"], "exception")
        affected = [item["id"] for item in manifest["revisions"] if item["key"] in issue["detail"]]
        citations = [
            source for revision_id in affected for source in manifest["sources"][revision_id]
        ]
        if issue["type"] in {"unconfirmed_claim", "conflicting_claims"}:
            citations = [
                source
                for group in manifest["sources"].values()
                for source in group
                if str(source["claim_id"]) in issue["detail"]
            ]
        questions.append(
            {
                "topic": topic,
                "question": f"Please resolve {issue['detail']} ({issue['type']}).",
                "citations": citations,
                "blocking": True,
            }
        )
    return sorted(questions, key=lambda question: QUESTION_ORDER.index(question["topic"]))


def propose_candidate(
    connection: psycopg.Connection, context: TenantContext, case_id: UUID, proposal: dict
) -> dict:
    _admin(context)
    if not isinstance(proposal, dict):
        raise TypeError("Structured candidate is required")
    with tenant_transaction(connection, context):
        case = connection.execute(
            "SELECT state FROM onboarding_cases WHERE organization_id = %s AND id = %s FOR UPDATE",
            (context.organization_id, case_id),
        ).fetchone()
        if case is None or case[0] == "locked_candidate":
            raise ValueError("An unlocked onboarding case is required")
        issues, manifest = _checks(connection, context.organization_id, proposal)
        canonical = {
            "name": str(proposal.get("name") or "Workflow draft").strip()[:80],
            "revision_ids": sorted(
                str(UUID(str(value))) for value in proposal.get("revision_ids", [])
            ),
            "edge_ids": sorted(str(UUID(str(value))) for value in proposal.get("edge_ids", [])),
            "behavior": proposal.get("behavior", {}),
            "acceptance_examples": proposal.get("acceptance_examples", []),
            "open_decisions": unique_decisions(proposal.get("open_decisions", [])),
            "clarifications": proposal.get("clarifications", []),
        }
        if proposal.get("suggested_definition"):
            canonical["suggested_definition"] = proposal["suggested_definition"]
        if requirements := proposal.get("email_requirements"):
            if set(requirements) - {"reply_mapping", "email_document", "automatic_email"}:
                raise ValueError("Unknown workflow email configuration")
            canonical["email_requirements"] = sorted(set(requirements))
        digest = _digest(canonical)
        candidate_id = connection.execute(
            "INSERT INTO blueprint_candidates (organization_id, case_id, proposal, content_hash, proposed_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (context.organization_id, case_id, Jsonb(canonical), digest, context.actor.user_id),
        ).fetchone()[0]
        target = "clarifying" if issues else "ready_for_review"
        connection.execute(
            "UPDATE onboarding_cases SET state = %s, revision = revision + 1, updated_at = now() "
            "WHERE organization_id = %s AND id = %s",
            (target, context.organization_id, case_id),
        )
        return {
            "id": candidate_id,
            "content_hash": digest,
            "state": target,
            "issues": issues,
            "questions": questions_for(issues, manifest),
        }


def review_candidate(
    connection: psycopg.Connection, context: TenantContext, case_id: UUID
) -> dict | None:
    _admin(context, "policies.read")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT id, proposal, content_hash, recorded_at FROM blueprint_candidates "
            "WHERE organization_id = %s AND case_id = %s ORDER BY candidate_order DESC LIMIT 1",
            (context.organization_id, case_id),
        ).fetchone()
        if row is None:
            return None
        previous = connection.execute(
            "SELECT proposal FROM blueprint_candidates WHERE organization_id = %s AND case_id = %s "
            "AND id <> %s ORDER BY candidate_order DESC LIMIT 1",
            (context.organization_id, case_id, row[0]),
        ).fetchone()
        issues, manifest = _checks(connection, context.organization_id, row[1])
        revisions, _ = _load_graph(
            connection,
            context.organization_id,
            [UUID(value) for value in row[1]["revision_ids"]],
            [UUID(value) for value in row[1]["edge_ids"]],
        )
        return {
            "id": row[0],
            "name": row[1].get("name") or "Workflow draft",
            "content_hash": row[2],
            "recorded_at": row[3],
            "behavior": row[1]["behavior"],
            "acceptance_examples": row[1]["acceptance_examples"],
            "open_decisions": unique_decisions(row[1].get("open_decisions", [])),
            "clarifications": row[1].get("clarifications", []),
            "suggested_definition": row[1].get("suggested_definition"),
            "email_requirements": row[1].get("email_requirements", []),
            "changes": _candidate_changes(previous[0], row[1]) if previous else [],
            "revisions": [
                {**item, "sources": manifest["sources"][item["id"]]} for item in revisions
            ],
            "issues": issues,
            "questions": questions_for(issues, manifest),
        }


def _candidate_changes(previous: dict, current: dict) -> list[str]:
    return [
        field
        for field in (
            "revision_ids",
            "edge_ids",
            "behavior",
            "acceptance_examples",
            "suggested_definition",
            "email_requirements",
            "open_decisions",
            "clarifications",
        )
        if previous.get(field) != current.get(field)
    ]


def answer_question(
    connection: psycopg.Connection,
    context: TenantContext,
    case_id: UUID,
    question_index: int,
    answer: str,
    content_hash: str | None = None,
) -> dict:
    _admin(context)
    if not answer.strip() or len(answer) > 5000:
        raise ValueError("A concise clarification answer is required")
    with tenant_transaction(connection, context):
        connection.execute("SELECT id FROM onboarding_cases WHERE organization_id = %s AND id = %s FOR UPDATE",
                           (context.organization_id, case_id))
        row = connection.execute(
            "SELECT proposal, content_hash FROM blueprint_candidates WHERE organization_id = %s AND case_id = %s "
            "ORDER BY candidate_order DESC LIMIT 1",
            (context.organization_id, case_id),
        ).fetchone()
        if row is None:
            raise LookupError("Candidate not found")
        if content_hash is not None and row[1] != content_hash:
            raise ValueError("The candidate changed. Review the current question.")
        issues, manifest = _checks(connection, context.organization_id, row[0])
        questions = questions_for(issues, manifest)
        if question_index < 0 or question_index >= len(questions):
            raise ValueError("Question is no longer current")
        revised = {
            **row[0],
            "clarifications": [
                *row[0].get("clarifications", []),
                {
                    "question": questions[question_index]["question"],
                    "answer": answer.strip(),
                    "answered_by": str(context.actor.user_id),
                    "answered_at": datetime.now(UTC).isoformat(),
                },
            ],
        }
        return propose_candidate(connection, context, case_id, revised)


def confirm_candidate(
    connection: psycopg.Connection, context: TenantContext, case_id: UUID, content_hash: str
) -> dict:
    _admin(context, "policies.approve")
    with tenant_transaction(connection, context):
        case = connection.execute(
            "SELECT state, revision FROM onboarding_cases WHERE organization_id = %s "
            "AND id = %s FOR UPDATE",
            (context.organization_id, case_id),
        ).fetchone()
        if case is None:
            raise LookupError("Onboarding case not found")
        if case[0] != "ready_for_review":
            raise ValueError("Candidate is not ready for review")
        row = connection.execute(
            "SELECT id, proposal, content_hash FROM blueprint_candidates WHERE organization_id = %s "
            "AND case_id = %s ORDER BY candidate_order DESC LIMIT 1",
            (context.organization_id, case_id),
        ).fetchone()
        if row is None or row[2] != content_hash or _digest(row[1]) != content_hash:
            raise ValueError("Candidate hash mismatch")
        issues, manifest = _checks(connection, context.organization_id, row[1])
        if issues:
            raise ValueError(f"Candidate has unresolved critical questions: {issues}")
        # Different workflow cases allocate from the same organization version sequence.
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (str(context.organization_id),))
        version = connection.execute(
            "SELECT coalesce(max(blueprint_version), 0) + 1 FROM blueprints "
            "WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        blueprint_id = connection.execute(
            "INSERT INTO blueprints (organization_id, blueprint_version, content_hash, confirmed_by) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (context.organization_id, version, content_hash, context.actor.user_id),
        ).fetchone()[0]
        for revision_id in row[1]["revision_ids"]:
            connection.execute(
                "INSERT INTO blueprint_members (organization_id, blueprint_id, revision_id) "
                "VALUES (%s, %s, %s)",
                (context.organization_id, blueprint_id, revision_id),
            )
            revision = connection.execute(
                "SELECT so.kind, so.stable_key, sr.content, sr.rationale "
                "FROM semantic_revisions sr JOIN semantic_objects so ON so.id = sr.object_id "
                "WHERE sr.organization_id = %s AND sr.id = %s",
                (context.organization_id, revision_id),
            ).fetchone()
            description = json.dumps(revision[:3], sort_keys=True, default=str) + revision[3]
            embedding_hash = hashlib.sha256(description.encode()).hexdigest()
            existing = connection.execute(
                "SELECT content_hash FROM semantic_revision_embeddings WHERE organization_id = %s "
                "AND revision_id = %s AND embedding_model = %s",
                (context.organization_id, revision_id, EMBEDDING_MODEL),
            ).fetchone()
            if existing and existing[0] != embedding_hash:
                raise ValueError("Immutable semantic revision embedding changed")
            if existing is None:
                connection.execute(
                    "INSERT INTO semantic_revision_embeddings (organization_id, revision_id, "
                    "embedding_model, content_hash, embedding) VALUES (%s, %s, %s, %s, %s)",
                    (
                        context.organization_id,
                        revision_id,
                        EMBEDDING_MODEL,
                        embedding_hash,
                        _embedding(description),
                    ),
                )
        for edge_id in row[1]["edge_ids"]:
            connection.execute(
                "INSERT INTO blueprint_edges (organization_id, blueprint_id, edge_id) "
                "VALUES (%s, %s, %s)",
                (context.organization_id, blueprint_id, edge_id),
            )
        connection.execute(
            "INSERT INTO blueprint_manifests (organization_id, blueprint_id, manifest) "
            "VALUES (%s, %s, %s)",
            (context.organization_id, blueprint_id, Jsonb(_serializable_manifest(manifest))),
        )
        connection.execute(
            "UPDATE onboarding_cases SET state = 'locked_candidate', revision = revision + 1, "
            "blueprint_id = %s, updated_at = now() WHERE organization_id = %s AND id = %s",
            (blueprint_id, context.organization_id, case_id),
        )
        correlation_id = uuid4()
        connection.execute(
            "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
            "resource_id, from_state, to_state, correlation_id) "
            "VALUES (%s, %s, 'onboarding', %s, 'ready_for_review', 'locked_candidate', %s)",
            (context.organization_id, context.actor.user_id, case_id, correlation_id),
        )
        connection.execute(
            "INSERT INTO runtime_events (organization_id, event_type, idempotency_key, "
            "correlation_id, payload) VALUES (%s, 'blueprint.candidate_confirmed', %s, %s, %s)",
            (
                context.organization_id,
                str(blueprint_id),
                correlation_id,
                Jsonb({"blueprint_id": str(blueprint_id), "content_hash": content_hash}),
            ),
        )
        return {
            "blueprint_id": blueprint_id,
            "content_hash": content_hash,
            "version": version,
            "case_revision": case[1] + 1,
        }


def _serializable_manifest(manifest: dict) -> dict:
    converted = {
        **manifest,
        "sources": {str(key): value for key, value in manifest["sources"].items()},
    }
    return json.loads(json.dumps(converted, default=str))
