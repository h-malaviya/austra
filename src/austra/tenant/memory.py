"""Tenant-scoped conversation and user memory persistence."""

from uuid import UUID, uuid4

import psycopg

from austra.files import SECRET_PATTERN
from austra.memory import MemoryUpdate
from austra.tenant.context import TenantContext, tenant_transaction


def memory_context(
    connection: psycopg.Connection,
    context: TenantContext,
    chat_kind: str,
    chat_id: UUID,
) -> dict:
    with tenant_transaction(connection, context):
        summary = connection.execute(
            "SELECT content, working_content, compacted_at, compacted_message_count "
            "FROM conversation_memory_summaries "
            "WHERE organization_id = %s AND chat_kind = %s AND chat_id = %s",
            (context.organization_id, chat_kind, chat_id),
        ).fetchone()
        facts = connection.execute(
            "SELECT content FROM conversation_memory_notes "
            "WHERE organization_id = %s AND chat_kind = %s AND chat_id = %s "
            "AND scope = 'chat' AND (%s::timestamptz IS NULL OR created_at > %s) "
            "GROUP BY content ORDER BY max(created_at) DESC",
            (
                context.organization_id,
                chat_kind,
                chat_id,
                summary[2] if summary else None,
                summary[2] if summary else None,
            ),
        ).fetchall()
        preferences = connection.execute(
            "SELECT content FROM conversation_memory_notes "
            "WHERE organization_id = %s AND owner_user_id = %s AND scope = 'user' "
            "GROUP BY content ORDER BY max(created_at) DESC",
            (context.organization_id, context.actor.user_id),
        ).fetchall()
        if chat_kind == "ask":
            pending_message_count = connection.execute(
                "SELECT count(*) FROM organization_chat_messages "
                "WHERE organization_id = %s AND chat_id = %s",
                (context.organization_id, chat_id),
            ).fetchone()[0]
        else:
            pending_message_count = connection.execute(
                "SELECT count(*) FROM onboarding_messages WHERE organization_id = %s",
                (context.organization_id,),
            ).fetchone()[0]
    return {
        "facts": [row[0] for row in facts],
        "user_preferences": [row[0] for row in preferences],
        "final_summary": summary[0] or "" if summary else "",
        "working_compaction": summary[1] if summary else "",
        "pending_message_count": max(0, pending_message_count - (summary[3] if summary else 0)),
    }


def save_memory_update(
    connection: psycopg.Connection,
    context: TenantContext,
    chat_kind: str,
    chat_id: UUID,
    message_id: UUID,
    source_body: str,
    update: MemoryUpdate,
    *,
    confirmed_summary: str = "",
    unresolved_questions: list[str] | None = None,
) -> None:
    summary = confirmed_summary.strip()
    if not summary and update.requirements_complete and not unresolved_questions:
        summary = update.final_summary.strip()
    if SECRET_PATTERN.search(summary):
        summary = ""
    working_compaction = update.working_compaction.strip()
    if SECRET_PATTERN.search(working_compaction):
        working_compaction = ""
    with tenant_transaction(connection, context):
        if chat_kind == "ask":
            active = connection.execute(
                "SELECT id FROM organization_chats WHERE id = %s AND organization_id = %s "
                "AND owner_user_id = %s AND deleted_at IS NULL FOR UPDATE",
                (chat_id, context.organization_id, context.actor.user_id),
            ).fetchone()
            if active is None:
                return
        compacted_at = None
        compacted_message_count = 0
        if working_compaction:
            compacted_at = connection.execute(
                "SELECT max(created_at) FROM conversation_memory_notes "
                "WHERE organization_id = %s AND chat_kind = %s AND chat_id = %s "
                "AND scope = 'chat'",
                (context.organization_id, chat_kind, chat_id),
            ).fetchone()[0]
            if chat_kind == "ask":
                compacted_message_count = connection.execute(
                    "SELECT count(*) FROM organization_chat_messages "
                    "WHERE organization_id = %s AND chat_id = %s",
                    (context.organization_id, chat_id),
                ).fetchone()[0]
            else:
                compacted_message_count = connection.execute(
                    "SELECT count(*) FROM onboarding_messages WHERE organization_id = %s",
                    (context.organization_id,),
                ).fetchone()[0]
        for note in update.notes:
            quote = note.source_quote.strip()
            content = note.text.strip()
            if (
                not quote
                or quote not in source_body
                or not content
                or SECRET_PATTERN.search(content)
            ):
                continue
            connection.execute(
                "INSERT INTO conversation_memory_notes "
                "(organization_id, owner_user_id, chat_kind, chat_id, "
                "source_message_id, scope, content) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    context.organization_id,
                    context.actor.user_id,
                    chat_kind,
                    chat_id,
                    message_id,
                    note.scope,
                    content,
                ),
            )
        if summary or working_compaction:
            connection.execute(
                "INSERT INTO conversation_memory_summaries "
                "(organization_id, owner_user_id, chat_kind, chat_id, "
                "source_message_id, content, working_content, compacted_at, "
                "compacted_message_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, chat_kind, chat_id) DO UPDATE SET "
                "owner_user_id = EXCLUDED.owner_user_id, "
                "source_message_id = EXCLUDED.source_message_id, "
                "content = coalesce(EXCLUDED.content, conversation_memory_summaries.content), "
                "working_content = CASE WHEN EXCLUDED.working_content <> '' "
                "THEN EXCLUDED.working_content ELSE conversation_memory_summaries.working_content END, "
                "compacted_at = coalesce(EXCLUDED.compacted_at, "
                "conversation_memory_summaries.compacted_at), "
                "compacted_message_count = CASE WHEN EXCLUDED.working_content <> '' "
                "THEN EXCLUDED.compacted_message_count "
                "ELSE conversation_memory_summaries.compacted_message_count END, "
                "updated_at = now()",
                (
                    context.organization_id,
                    context.actor.user_id,
                    chat_kind,
                    chat_id,
                    message_id,
                    summary or None,
                    working_compaction,
                    compacted_at,
                    compacted_message_count,
                ),
            )


def list_chats(connection: psycopg.Connection, context: TenantContext) -> list[dict]:
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT id, title, updated_at FROM organization_chats "
            "WHERE organization_id = %s AND owner_user_id = %s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC",
            (context.organization_id, context.actor.user_id),
        ).fetchall()
    return [
        {"id": str(chat_id), "title": title, "updated_at": updated_at.isoformat()}
        for chat_id, title, updated_at in rows
    ]


def chat(connection: psycopg.Connection, context: TenantContext, chat_id: UUID) -> dict:
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT title, created_at, updated_at FROM organization_chats "
            "WHERE id = %s AND organization_id = %s AND owner_user_id = %s "
            "AND deleted_at IS NULL",
            (chat_id, context.organization_id, context.actor.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("Chat not found")
        messages = connection.execute(
            "SELECT id, role, body, created_at FROM organization_chat_messages "
            "WHERE organization_id = %s AND chat_id = %s ORDER BY created_at, id",
            (context.organization_id, chat_id),
        ).fetchall()
    return {
        "id": str(chat_id),
        "title": row[0],
        "source": "organization_ask",
        "created_at": row[1].isoformat(),
        "updated_at": row[2].isoformat(),
        "messages": [
            {"id": str(message_id), "role": role, "body": body, "at": at.isoformat()}
            for message_id, role, body, at in messages
        ],
    }


def add_user_message(
    connection: psycopg.Connection,
    context: TenantContext,
    question: str,
    chat_id: UUID | None,
) -> tuple[UUID, UUID]:
    with tenant_transaction(connection, context):
        if chat_id is None:
            chat_id = uuid4()
            connection.execute(
                "INSERT INTO organization_chats (id, organization_id, owner_user_id, title) "
                "VALUES (%s, %s, %s, %s)",
                (chat_id, context.organization_id, context.actor.user_id, question[:80]),
            )
        else:
            row = connection.execute(
                "SELECT id FROM organization_chats WHERE id = %s AND organization_id = %s "
                "AND owner_user_id = %s AND deleted_at IS NULL FOR UPDATE",
                (chat_id, context.organization_id, context.actor.user_id),
            ).fetchone()
            if row is None:
                raise LookupError("Chat not found")
        message_id = uuid4()
        connection.execute(
            "INSERT INTO organization_chat_messages "
            "(id, organization_id, chat_id, owner_user_id, role, body) "
            "VALUES (%s, %s, %s, %s, 'user', %s)",
            (message_id, context.organization_id, chat_id, context.actor.user_id, question),
        )
        connection.execute(
            "UPDATE organization_chats SET updated_at = now() WHERE id = %s", (chat_id,)
        )
    return chat_id, message_id


def add_assistant_message(
    connection: psycopg.Connection,
    context: TenantContext,
    chat_id: UUID,
    answer: str,
) -> UUID:
    message_id = uuid4()
    with tenant_transaction(connection, context):
        active = connection.execute(
            "SELECT id FROM organization_chats WHERE id = %s AND organization_id = %s "
            "AND owner_user_id = %s AND deleted_at IS NULL FOR UPDATE",
            (chat_id, context.organization_id, context.actor.user_id),
        ).fetchone()
        if active is None:
            raise LookupError("Chat not found")
        connection.execute(
            "INSERT INTO organization_chat_messages "
            "(id, organization_id, chat_id, owner_user_id, role, body) "
            "VALUES (%s, %s, %s, %s, 'assistant', %s)",
            (message_id, context.organization_id, chat_id, context.actor.user_id, answer),
        )
        connection.execute(
            "UPDATE organization_chats SET updated_at = now() WHERE id = %s", (chat_id,)
        )
    return message_id


def delete_chat(connection: psycopg.Connection, context: TenantContext, chat_id: UUID) -> None:
    with tenant_transaction(connection, context):
        row = connection.execute(
            "UPDATE organization_chats SET deleted_at = now() "
            "WHERE id = %s AND organization_id = %s AND owner_user_id = %s "
            "AND deleted_at IS NULL RETURNING id",
            (chat_id, context.organization_id, context.actor.user_id),
        ).fetchone()
        if row is None:
            raise LookupError("Chat not found")
        connection.execute(
            "DELETE FROM conversation_memory_notes WHERE organization_id = %s "
            "AND chat_kind = 'ask' AND chat_id = %s",
            (context.organization_id, chat_id),
        )
        connection.execute(
            "DELETE FROM conversation_memory_summaries WHERE organization_id = %s "
            "AND chat_kind = 'ask' AND chat_id = %s",
            (context.organization_id, chat_id),
        )
