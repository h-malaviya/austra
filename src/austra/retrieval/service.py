"""Resolve authorized versions before exact and hybrid evidence retrieval."""

import re
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

import psycopg

from austra.onboarding.evidence import EMBEDDING_MODEL, _embedding
from austra.tenant.context import TenantContext, tenant_transaction

LANE_LIMIT = 30
PASSAGE_LIMIT = 8
EVIDENCE_TOKEN_LIMIT = 4000
FUSION_OFFSET = 60
IDENTIFIER = re.compile(r"\b[A-Za-z]{1,12}[-_][A-Za-z0-9_-]{2,64}\b")
WORDS = re.compile(r"[a-z0-9_]{3,}")
STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "how",
        "what",
        "why",
        "who",
        "when",
        "was",
        "are",
        "does",
        "did",
        "can",
        "our",
        "with",
        "from",
        "this",
        "that",
    }
)
KINDS = (
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
)


def classify(question: str, requested_time: datetime | None) -> str:
    lower = question.casefold()
    if "compare" in lower or "difference" in lower or "changed" in lower:
        return "comparison"
    if requested_time or "historical" in lower or "at the time" in lower:
        return "history"
    if "total" in lower or "sum" in lower or "how many" in lower:
        return "current_fact"
    if any(word in lower for word in ("why", "how", "explain", "evidence")):
        return "explanation"
    if any(word in lower for word in ("approve", "send", "create", "reserve")):
        return "action"
    return "discovery"


def _visible_sql() -> str:
    return (
        "WITH visible_revisions AS ("
        "SELECT DISTINCT r.id FROM evidence_revisions r "
        "JOIN evidence_sources s ON s.id = r.source_id AND s.organization_id = r.organization_id "
        "WHERE r.organization_id = %(organization_id)s AND r.status = 'complete' "
        "AND r.captured_at <= %(recorded_time)s AND s.excluded = false "
        "AND EXISTS (SELECT 1 FROM source_claims cl "
        "JOIN evidence_chunks support ON support.id = cl.chunk_id "
        "JOIN semantic_revision_claims link ON link.claim_id = cl.id "
        "JOIN blueprint_members member ON member.revision_id = link.revision_id "
        "WHERE support.revision_id = r.id AND member.blueprint_id = %(blueprint_id)s "
        "AND member.organization_id = r.organization_id "
        "AND (SELECT decision FROM claim_decisions d WHERE d.claim_id = cl.id "
        "AND d.recorded_at <= %(recorded_time)s "
        "ORDER BY d.decision_order DESC LIMIT 1) = 'confirmed')) "
    )


def _semantic_visible() -> str:
    return (
        "AND NOT EXISTS (SELECT 1 FROM semantic_revision_claims link "
        "JOIN source_claims claim ON claim.id = link.claim_id "
        "LEFT JOIN evidence_chunks support ON support.id = claim.chunk_id "
        "LEFT JOIN evidence_revisions source_revision ON source_revision.id = support.revision_id "
        "WHERE link.revision_id = sr.id AND (support.id IS NULL "
        "OR source_revision.status <> 'complete' "
        "OR source_revision.captured_at > %(recorded_time)s "
        "OR (SELECT decision FROM claim_decisions d WHERE d.claim_id = claim.id "
        "AND d.recorded_at <= %(recorded_time)s "
        "ORDER BY d.decision_order DESC LIMIT 1) IS DISTINCT FROM 'confirmed')) "
    )


def _rows(connection: psycopg.Connection, query: str, parameters: dict) -> list[dict]:
    cursor = connection.execute(query, parameters)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _fuse(lanes: list[list[dict]]) -> list[dict]:
    ranked: dict[UUID, dict] = {}
    for lane in lanes:
        for position, row in enumerate(lane, 1):
            item = ranked.setdefault(row["id"], {**row, "score": 0.0})
            item["score"] += 1 / (FUSION_OFFSET + position)
    return sorted(ranked.values(), key=lambda row: (-row["score"], str(row["id"])))


def _covered(question: str, exact: list[dict], passages: list[dict]) -> bool:
    if exact:
        return True
    text = " ".join(row["text"] for row in passages).casefold()
    if not text:
        return False
    clauses = re.split(r"\s+(?:and|also)\s+|[;?]", question.casefold())
    for clause in clauses:
        terms = {word for word in WORDS.findall(clause) if word not in STOP_WORDS}
        if terms and sum(word in text for word in terms) < min(2, len(terms)):
            return False
    return True


def retrieve(
    connection: psycopg.Connection,
    context: TenantContext,
    question: str,
    *,
    department_id: UUID | None = None,
    blueprint_id: UUID | None = None,
    valid_time: datetime | None = None,
    recorded_time: datetime | None = None,
) -> dict:
    """Return only current, approved, permission-visible evidence with citations."""
    if not question.strip() or len(question) > 1000:
        raise ValueError("Question must have 1 to 1000 characters")
    context.require("evidence.read", department_id)
    context.require("policies.read", department_id)
    if "total" in question.casefold() or "how many" in question.casefold():
        context.require("records.read", department_id)
    if valid_time and valid_time.tzinfo is None or recorded_time and recorded_time.tzinfo is None:
        raise ValueError("Times must include an offset")
    historical = valid_time is not None or blueprint_id is not None
    valid_time = valid_time or datetime.now(UTC)
    recorded_time = recorded_time or datetime.now(UTC)
    started = perf_counter()
    plan = classify(question, valid_time if historical else None)
    parameters = {
        "organization_id": context.organization_id,
        "blueprint_id": blueprint_id,
        "valid_time": valid_time,
        "recorded_time": recorded_time,
        "question": question,
        "embedding_model": EMBEDDING_MODEL,
        "query_embedding": _embedding(question),
    }
    with tenant_transaction(connection, context):
        aggregate = None
        if re.search(r"\b(?:total|how many)\s+(?:active\s+)?records\b", question.casefold()):
            if historical:
                return {
                    "status": "unknown",
                    "reason": "Historical record totals are unavailable",
                    "plan": plan,
                    "passages": [],
                    "citations": [],
                    "coverage": False,
                }
            count = connection.execute(
                "SELECT count(*) FROM business_objects WHERE organization_id = %s "
                "AND kind = 'record' AND archived_at IS NULL "
                "AND (%s::uuid IS NULL OR department_id = %s::uuid)",
                (context.organization_id, department_id, department_id),
            ).fetchone()[0]
            aggregate = {"measure": "active_record_count", "value": count, "method": "sql_count"}
        if blueprint_id:
            blueprint = connection.execute(
                "SELECT id, blueprint_version FROM blueprints WHERE organization_id = %s "
                "AND id = %s AND status IN ('active', 'superseded') "
                "AND confirmed_at <= %s",
                (context.organization_id, blueprint_id, recorded_time),
            ).fetchone()
        else:
            blueprint = connection.execute(
                "SELECT id, blueprint_version FROM blueprints WHERE organization_id = %s "
                "AND status IN ('active', 'superseded') AND confirmed_at <= %s "
                "AND coalesce(effective_at, confirmed_at) <= %s "
                "ORDER BY coalesce(effective_at, confirmed_at) DESC, blueprint_version DESC LIMIT 1",
                (context.organization_id, recorded_time, valid_time),
            ).fetchone()
        if blueprint is None:
            return {
                "status": "unknown",
                "reason": "No applicable published blueprint",
                "plan": plan,
                "passages": [],
                "citations": [],
                "coverage": False,
            }
        parameters["blueprint_id"] = blueprint[0]
        if aggregate is not None:
            return {
                "status": "supported",
                "plan": plan,
                "blueprint_id": blueprint[0],
                "aggregate": aggregate,
                "passages": [],
                "citations": [],
                "coverage": True,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
            }
        if "total" in question.casefold() or "how many" in question.casefold():
            return {
                "status": "unknown",
                "reason": "No typed SQL aggregate for this total",
                "plan": plan,
                "blueprint_id": blueprint[0],
                "passages": [],
                "citations": [],
                "coverage": False,
            }
        conflicts = connection.execute(
            "SELECT count(*) FROM claim_conflicts cc "
            "JOIN source_claims first_claim ON first_claim.id = cc.first_claim_id "
            "JOIN source_claims second_claim ON second_claim.id = cc.second_claim_id "
            "JOIN evidence_chunks first_chunk ON first_chunk.id = first_claim.chunk_id "
            "JOIN evidence_chunks second_chunk ON second_chunk.id = second_claim.chunk_id "
            "WHERE cc.organization_id = %s AND EXISTS ("
            "SELECT 1 FROM semantic_revision_claims src JOIN blueprint_members bm "
            "ON bm.revision_id = src.revision_id WHERE bm.blueprint_id = %s "
            "AND src.claim_id = cc.first_claim_id) AND EXISTS ("
            "SELECT 1 FROM semantic_revision_claims src JOIN blueprint_members bm "
            "ON bm.revision_id = src.revision_id WHERE bm.blueprint_id = %s "
            "AND src.claim_id = cc.second_claim_id)",
            (context.organization_id, blueprint[0], blueprint[0]),
        ).fetchone()[0]
        if conflicts:
            return {
                "status": "unknown",
                "reason": "Approved sources conflict",
                "plan": plan,
                "blueprint_id": blueprint[0],
                "passages": [],
                "citations": [],
                "coverage": False,
                "conflict_count": conflicts,
            }
        identifiers = IDENTIFIER.findall(question)
        exact = _rows(
            connection,
            "SELECT sr.id, so.kind, so.stable_key, sr.content, sr.rationale, "
            "sr.valid_from, sr.valid_to, sr.recorded_at "
            "FROM semantic_objects so JOIN semantic_revisions sr ON sr.object_id = so.id "
            "JOIN blueprint_members bm ON bm.revision_id = sr.id "
            "WHERE so.organization_id = %(organization_id)s AND bm.blueprint_id = %(blueprint_id)s "
            "AND sr.valid_from <= %(valid_time)s "
            "AND (sr.valid_to IS NULL OR sr.valid_to > %(valid_time)s) "
            "AND sr.recorded_at <= %(recorded_time)s "
            + _semantic_visible()
            + "AND (so.stable_key = ANY(%(identifiers)s) OR so.stable_key = %(question)s) "
            "ORDER BY so.kind, so.stable_key LIMIT 8",
            {
                **parameters,
                "identifiers": [item.casefold().replace("-", "_") for item in identifiers],
            },
        )
        record_matches = []
        if identifiers and not exact:
            context.require("records.read", department_id)
            record_matches = _rows(
                connection,
                "SELECT id, kind, external_id, department_id FROM business_objects "
                "WHERE organization_id = %(organization_id)s AND archived_at IS NULL "
                "AND external_id = ANY(%(identifiers)s) LIMIT 8",
                {**parameters, "identifiers": identifiers},
            )
        common = _visible_sql()
        columns = (
            "SELECT c.id, c.revision_id, c.locator, c.text, c.content_hash, "
            "c.token_count, s.display_name, s.kind AS source_kind, r.captured_at "
            "FROM evidence_chunks c JOIN visible_revisions v ON v.id = c.revision_id "
            "JOIN evidence_revisions r ON r.id = c.revision_id "
            "JOIN evidence_sources s ON s.id = r.source_id "
        )
        lexical = _rows(
            connection,
            common
            + columns
            + "WHERE c.organization_id = %(organization_id)s AND c.search_vector @@ "
            "websearch_to_tsquery('english', %(question)s) "
            "ORDER BY ts_rank_cd(c.search_vector, websearch_to_tsquery('english', %(question)s)) "
            "DESC, c.id LIMIT 30",
            parameters,
        )
        if not lexical:
            terms = sorted(
                {word for word in WORDS.findall(question.casefold()) if word not in STOP_WORDS}
            )[:12]
            if terms:
                broad = {**parameters, "term_query": " | ".join(terms)}
                lexical = _rows(
                    connection,
                    common
                    + columns
                    + "WHERE c.organization_id = %(organization_id)s AND c.search_vector @@ "
                    "to_tsquery('english', %(term_query)s) "
                    "ORDER BY ts_rank_cd(c.search_vector, to_tsquery('english', %(term_query)s)) "
                    "DESC, c.id LIMIT 30",
                    broad,
                )
        vector = _rows(
            connection,
            common
            + columns
            + "WHERE c.organization_id = %(organization_id)s AND c.embedding_model = %(embedding_model)s "
            "AND c.embedding IS NOT NULL "
            "AND c.embedding OPERATOR(public.<=>) %(query_embedding)s::public.vector <= 0.8 "
            "ORDER BY c.embedding OPERATOR(public.<=>) %(query_embedding)s::public.vector, "
            "c.id LIMIT 30",
            parameters,
        )
        semantic = _rows(
            connection,
            "SELECT sr.id, so.kind, so.stable_key, sr.content, sr.rationale, "
            "sr.valid_from, sr.valid_to, sr.recorded_at FROM semantic_revision_embeddings se "
            "JOIN semantic_revisions sr ON sr.id = se.revision_id "
            "JOIN semantic_objects so ON so.id = sr.object_id "
            "JOIN blueprint_members bm ON bm.revision_id = sr.id "
            "WHERE se.organization_id = %(organization_id)s "
            "AND se.embedding_model = %(embedding_model)s "
            "AND bm.blueprint_id = %(blueprint_id)s AND sr.valid_from <= %(valid_time)s "
            "AND (sr.valid_to IS NULL OR sr.valid_to > %(valid_time)s) "
            "AND sr.recorded_at <= %(recorded_time)s "
            + _semantic_visible()
            + "AND se.embedding OPERATOR(public.<=>) %(query_embedding)s::public.vector <= 0.8 "
            "ORDER BY se.embedding OPERATOR(public.<=>) %(query_embedding)s::public.vector "
            "LIMIT 3",
            parameters,
        )
    seen_hashes = set()
    passages = []
    token_count = 0
    for row in _fuse([lexical, vector]):
        if row["content_hash"] in seen_hashes or len(passages) >= PASSAGE_LIMIT:
            continue
        if token_count + row["token_count"] > EVIDENCE_TOKEN_LIMIT:
            continue
        seen_hashes.add(row["content_hash"])
        token_count += row["token_count"]
        passages.append(
            {
                key: row[key]
                for key in (
                    "id",
                    "revision_id",
                    "locator",
                    "text",
                    "display_name",
                    "source_kind",
                    "captured_at",
                )
            }
        )
    covered = _covered(question, exact, passages) or bool(
        record_matches and re.search(r"\b(?:exist|exists|find|locate)\b", question.casefold())
    )
    if identifiers and not exact and not record_matches:
        covered = False
    return {
        "status": "supported" if covered else "unknown",
        "reason": ""
        if covered
        else "No approved visible evidence covers every part of this question",
        "plan": plan,
        "organization_id": context.organization_id,
        "actor_id": context.actor.user_id,
        "department_id": department_id,
        "blueprint_id": blueprint[0],
        "blueprint_version": blueprint[1],
        "valid_time": valid_time,
        "recorded_time": recorded_time,
        "exact": exact,
        "exact_records": record_matches,
        "semantic": semantic,
        "passages": passages,
        "citations": [row["id"] for row in passages],
        "coverage": covered,
        "selected_tokens": token_count,
        "candidate_counts": {"lexical": len(lexical), "vector": len(vector)},
        "latency_ms": round((perf_counter() - started) * 1000, 2),
    }
