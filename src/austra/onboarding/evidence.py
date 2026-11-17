"""Tenant scoped onboarding evidence, revisions, and resumable extraction jobs."""

import csv
import hashlib
import io
import itertools
import math
import os
import re
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from openpyxl.utils.exceptions import InvalidFileException
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from austra.onboarding.extract import chunks, units, validate_file
from austra.onboarding.website import parse_page
from austra.tenant.context import TenantContext, tenant_transaction

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_UNITS_PER_JOB = 16
MAX_PDF_PAGES_PER_JOB = 2
EMBEDDING_MODEL = "local_feature_hash_v1"
EMBEDDING_DIMS = 384


def blob_root() -> Path:
    workspace = Path(os.environ.get("AUSTRA_WORKSPACE", "data/blank"))
    return workspace / "evidence_blobs"


def _store_bytes(root: Path, organization_id: UUID, digest: str, content: bytes) -> str:
    directory = root / str(organization_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / digest
    if not path.exists():
        temporary = directory / f".{uuid4().hex}.upload"
        try:
            with temporary.open("xb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("Stored source checksum mismatch")
    return f"{organization_id}/{digest}"


def _load_bytes(root: Path, organization_id: UUID, blob_key: str, digest: str) -> bytes:
    if blob_key != f"{organization_id}/{digest}":
        raise ValueError("Invalid tenant blob key")
    content = (root / blob_key).read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Stored source checksum mismatch")
    return content


def _embedding(content: str) -> str:
    values = [0.0] * EMBEDDING_DIMS
    for word in re.findall(r"\w+", content.casefold()):
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMS
        values[index] += 1 if digest[4] & 1 else -1
    norm = math.sqrt(sum(value * value for value in values)) or 1
    return "[" + ",".join(f"{value / norm:.6f}" for value in values) + "]"


def save_source(
    connection: psycopg.Connection,
    context: TenantContext,
    root: Path,
    *,
    kind: str,
    source_key: str,
    display_name: str,
    media_type: str,
    content: bytes,
) -> dict:
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise ValueError("Source must be nonempty and at most 25 MB")
    if not source_key.strip() or len(source_key) > 2048:
        raise ValueError("Invalid source name")
    digest = hashlib.sha256(content).hexdigest()
    blob_key = _store_bytes(root, context.organization_id, digest, content)
    with tenant_transaction(connection, context):
        case = connection.execute(
            "SELECT id, state FROM onboarding_cases WHERE organization_id = %s "
            "ORDER BY (state <> 'locked_candidate') DESC, created_at DESC "
            "LIMIT 1 FOR UPDATE",
            (context.organization_id,),
        ).fetchone()
        if case is None:
            raise ValueError("An onboarding case is required")
        if (
            case[1] == "locked_candidate"
            and connection.execute(
                "SELECT 1 FROM releases WHERE organization_id = %s AND state = 'active'",
                (context.organization_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("An open onboarding case or active release is required")
        source = connection.execute(
            "INSERT INTO evidence_sources (organization_id, case_id, kind, source_key, display_name) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (organization_id, kind, source_key) "
            "DO UPDATE SET display_name = excluded.display_name, "
            "case_id = excluded.case_id RETURNING id",
            (context.organization_id, case[0], kind, source_key, display_name),
        ).fetchone()[0]
        previous = connection.execute(
            "SELECT id, revision, sha256 FROM evidence_revisions WHERE organization_id = %s "
            "AND source_id = %s ORDER BY revision DESC LIMIT 1 FOR UPDATE",
            (context.organization_id, source),
        ).fetchone()
        if previous and previous[2] == digest:
            return {
                "source_id": source,
                "revision_id": previous[0],
                "revision": previous[1],
                "changed": False,
            }
        number = previous[1] + 1 if previous else 1
        revision = connection.execute(
            "INSERT INTO evidence_revisions (organization_id, source_id, revision, sha256, "
            "blob_key, byte_count, media_type) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (context.organization_id, source, number, digest, blob_key, len(content), media_type),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO evidence_jobs (organization_id, revision_id) VALUES (%s, %s)",
            (context.organization_id, revision),
        )
        from austra.build_release.monitoring import open_review

        active = connection.execute(
            "SELECT blueprint_id, approved_by FROM releases WHERE organization_id = %s "
            "AND state = 'active'",
            (context.organization_id,),
        ).fetchone()
        if active:
            open_review(
                connection,
                context,
                trigger="changed_evidence",
                subject_id=revision,
                owner_user_id=active[1],
                blueprint_id=active[0],
                citations=[
                    {
                        "source_id": str(source),
                        "prior_revision_id": str(previous[0]) if previous else None,
                        "revision_id": str(revision),
                    }
                ],
                impact="Evidence changed after the active blueprint was released.",
                next_step="Review the evidence and clarify any affected business meaning.",
            )
        if case[1] == "created":
            connection.execute(
                "UPDATE onboarding_cases SET state = 'collecting', revision = revision + 1, "
                "updated_at = now() WHERE organization_id = %s AND id = %s",
                (context.organization_id, case[0]),
            )
        return {"source_id": source, "revision_id": revision, "revision": number, "changed": True}


def save_file(connection, context, root, filename: str, content: bytes, *, linkedin=False):
    name = Path(filename.replace("\\", "/")).name
    media_type = validate_file(name, content)
    if linkedin and not name.lower().endswith((".csv", ".xlsx", ".txt", ".pdf", ".docx")):
        raise ValueError("Unsupported LinkedIn export")
    return save_source(
        connection,
        context,
        root,
        kind="linkedin_export" if linkedin else "file",
        source_key=name.casefold(),
        display_name=name,
        media_type=media_type,
        content=content,
    )


def set_source_access(
    connection,
    context: TenantContext,
    source_id: UUID,
    *,
    access_label: str,
    department_id: UUID | None = None,
    owner_user_id: UUID | None = None,
) -> None:
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    if access_label not in {"organization", "department", "owner", "restricted"}:
        raise ValueError("Invalid access label")
    if access_label == "department":
        if (
            department_id is None
            or not connection.execute(
                "SELECT 1 FROM departments WHERE organization_id = %s AND id = %s "
                "AND archived_at IS NULL",
                (context.organization_id, department_id),
            ).fetchone()
        ):
            raise ValueError("Active department required")
    elif department_id is not None:
        raise ValueError("Department is only valid for department access")
    if access_label == "owner":
        if (
            owner_user_id is None
            or not connection.execute(
                "SELECT 1 FROM users WHERE organization_id = %s AND id = %s AND status = 'active'",
                (context.organization_id, owner_user_id),
            ).fetchone()
        ):
            raise ValueError("Active owner required")
    elif owner_user_id is not None:
        raise ValueError("Owner is only valid for owner access")
    with tenant_transaction(connection, context):
        changed = connection.execute(
            "UPDATE evidence_sources SET access_label = %s, department_id = %s, "
            "owner_user_id = %s WHERE organization_id = %s AND id = %s RETURNING id",
            (access_label, department_id, owner_user_id, context.organization_id, source_id),
        ).fetchone()
        if changed is None:
            raise LookupError("Evidence source not found")


def save_manual(connection, context, root, text: str):
    if not text.strip():
        raise ValueError("LinkedIn Page text is required")
    return save_source(
        connection,
        context,
        root,
        kind="linkedin_manual",
        source_key="admin_supplied_page_text",
        display_name="LinkedIn Page text",
        media_type="text/plain",
        content=text.encode("utf-8"),
    )


def save_website_page(connection, context, root, url: str, content: bytes):
    return save_source(
        connection,
        context,
        root,
        kind="website",
        source_key=url,
        display_name=url,
        media_type="text/html",
        content=content,
    )


def save_interview(connection, context: TenantContext, description: str, outcome: str) -> dict:
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    description, outcome = description.strip(), outcome.strip()
    if not description or not outcome or len(description) > 10000 or len(outcome) > 10000:
        raise ValueError(
            "Business description and desired outcome are required (10,000 characters max)"
        )
    with tenant_transaction(connection, context):
        row = connection.execute(
            "UPDATE onboarding_cases SET business_description = %s, desired_outcome = %s, "
            "state = CASE WHEN state = 'created' THEN 'collecting' ELSE state END, "
            "revision = revision + 1, updated_at = now() WHERE organization_id = %s "
            "AND state IN ('created', 'collecting', 'clarifying') RETURNING id, state, revision",
            (description, outcome, context.organization_id),
        ).fetchone()
    if row is None:
        raise ValueError("An open onboarding case is required")
    return {"id": row[0], "state": row[1], "revision": row[2]}


def _source_units(media_type: str, content: bytes, url: str):
    if media_type == "text/html":
        page = parse_page(content)
        yield url, " ".join(page.text)
    else:
        yield from units(media_type, content)


def process_job(connection, context: TenantContext, root: Path, revision_id: UUID) -> dict:
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT r.blob_key, r.sha256, r.media_type, s.source_key, j.cursor, j.state "
            "FROM evidence_jobs j JOIN evidence_revisions r ON r.id = j.revision_id "
            "JOIN evidence_sources s ON s.id = r.source_id "
            "WHERE j.organization_id = %s AND j.revision_id = %s FOR UPDATE OF j",
            (context.organization_id, revision_id),
        ).fetchone()
        if row is None:
            raise LookupError("Evidence revision not found")
        blob_key, digest, media_type, url, cursor, state = row
        if state == "complete":
            return {"revision_id": revision_id, "state": state, "extracted_units": cursor}
        limit = MAX_PDF_PAGES_PER_JOB if media_type == "application/pdf" else MAX_UNITS_PER_JOB
        try:
            content = _load_bytes(root, context.organization_id, blob_key, digest)
            iterator = _source_units(media_type, content, url)
            if media_type == "application/pdf":
                total = len(PdfReader(io.BytesIO(content)).pages)
                batch = list(itertools.islice(iterator, cursor, cursor + limit))
                complete = cursor + len(batch) >= total
            else:
                batch = list(itertools.islice(iterator, cursor, cursor + limit + 1))
                complete = len(batch) <= limit
        except (
            ValueError,
            OSError,
            RuntimeError,
            UnicodeError,
            csv.Error,
            zipfile.BadZipFile,
            InvalidFileException,
            PdfReadError,
        ) as error:
            failure = "partial" if cursor else "failed"
            connection.execute(
                "UPDATE evidence_jobs SET state = %s, attempts = attempts + 1, updated_at = now() "
                "WHERE organization_id = %s AND revision_id = %s",
                (failure, context.organization_id, revision_id),
            )
            connection.execute(
                "UPDATE evidence_revisions SET status = %s, error = %s "
                "WHERE organization_id = %s AND id = %s",
                (failure, str(error)[:500], context.organization_id, revision_id),
            )
            return {
                "revision_id": revision_id,
                "state": failure,
                "extracted_units": cursor,
                "error": str(error)[:500],
            }
        selected = batch[:limit]
        for locator, value in selected:
            for chunk_locator, text in chunks(locator, value):
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                old = connection.execute(
                    "SELECT embedding FROM evidence_chunks WHERE organization_id = %s "
                    "AND content_hash = %s AND embedding_model = %s LIMIT 1",
                    (context.organization_id, content_hash, EMBEDDING_MODEL),
                ).fetchone()
                embedding = old[0] if old else _embedding(text)
                connection.execute(
                    "INSERT INTO evidence_chunks (organization_id, revision_id, locator, text, "
                    "content_hash, token_count, embedding, embedding_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        context.organization_id,
                        revision_id,
                        chunk_locator,
                        text,
                        content_hash,
                        len(text.split()),
                        embedding,
                        EMBEDDING_MODEL,
                    ),
                )
        next_cursor = cursor + len(selected)
        next_state = "complete" if complete else "pending"
        connection.execute(
            "UPDATE evidence_jobs SET cursor = %s, state = %s, attempts = attempts + 1, "
            "updated_at = now() WHERE organization_id = %s AND revision_id = %s",
            (next_cursor, next_state, context.organization_id, revision_id),
        )
        connection.execute(
            "UPDATE evidence_revisions SET status = %s, extracted_units = %s, "
            "total_units = %s, error = NULL WHERE organization_id = %s AND id = %s",
            (
                next_state,
                next_cursor,
                next_cursor if complete else None,
                context.organization_id,
                revision_id,
            ),
        )
        return {
            "revision_id": revision_id,
            "state": next_state,
            "extracted_units": next_cursor,
            "total_units": next_cursor if complete else None,
        }


def list_sources(connection, context: TenantContext) -> list[dict]:
    context.require("policies.read")
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT s.id, s.kind, s.display_name, s.excluded, r.id, r.revision, r.sha256, "
            "r.captured_at, r.status, r.extracted_units, r.total_units, r.error, "
            "(SELECT count(*) FROM evidence_chunks c WHERE c.organization_id = s.organization_id "
            "AND c.revision_id = r.id) FROM evidence_sources s "
            "JOIN LATERAL (SELECT * FROM evidence_revisions WHERE organization_id = s.organization_id "
            "AND source_id = s.id ORDER BY revision DESC LIMIT 1) r ON true "
            "WHERE s.organization_id = %s ORDER BY s.created_at DESC",
            (context.organization_id,),
        ).fetchall()
    return [
        dict(
            zip(
                (
                    "id",
                    "kind",
                    "name",
                    "excluded",
                    "revision_id",
                    "revision",
                    "sha256",
                    "captured_at",
                    "status",
                    "extracted_units",
                    "total_units",
                    "error",
                    "chunk_count",
                ),
                row,
                strict=True,
            )
        )
        for row in rows
    ]


def preview_source(connection, context: TenantContext, source_id: UUID) -> list[dict]:
    context.require("policies.read")
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT c.locator, left(c.text, 1200), r.captured_at FROM evidence_sources s "
            "JOIN LATERAL (SELECT id, captured_at FROM evidence_revisions WHERE organization_id = s.organization_id "
            "AND source_id = s.id ORDER BY revision DESC LIMIT 1) r ON true "
            "JOIN evidence_chunks c ON c.organization_id = s.organization_id AND c.revision_id = r.id "
            "WHERE s.organization_id = %s AND s.id = %s ORDER BY c.locator LIMIT 30",
            (context.organization_id, source_id),
        ).fetchall()
    return [
        {"locator": locator, "text": text, "captured_at": captured_at}
        for locator, text, captured_at in rows
    ]


def exclude_source(connection, context: TenantContext, source_id: UUID, excluded: bool) -> None:
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "UPDATE evidence_sources SET excluded = %s WHERE organization_id = %s AND id = %s RETURNING id",
            (excluded, context.organization_id, source_id),
        ).fetchone()
        if row is None:
            raise LookupError("Evidence source not found")
