"""Find released workflows and persist the selected workflow in an owned conversation."""

from uuid import UUID

from psycopg.types.json import Jsonb

from austra.capabilities.runtime import active_builds, allows_department, department_capabilities
from austra.tenant.context import tenant_transaction
from austra.tenant.memory import add_assistant_message, add_user_message, chat, list_chats
from austra.tenant.permissions.evaluator import permits


def released_workflow_catalog(db, context):
    departments = db.execute(
        "SELECT id, name FROM departments WHERE organization_id = %s AND archived_at IS NULL ORDER BY name",
        (context.organization_id,),
    ).fetchall()
    allowed = [(identifier, name) for identifier, name in departments if permits(
        context.actor, context.organization_id, identifier, "workflows.read"
    )]
    with tenant_transaction(db, context):
        builds = active_builds(db, context)
    return [
        {"build_id": str(build["build_id"]), "name": build["name"] or "Business workflow",
         "department_id": str(identifier), "department": name}
        for build in builds for identifier, name in allowed
        if allows_department(build["manifest"], identifier)
        and (build["manifest"].get("engine") != "state_machine"
             or department_capabilities(build["manifest"], identifier))
    ]


def workflow_chats(db, context):
    return [item for item in list_chats(db, context) if "workflow" in item]


def start_workflow_chat(db, context, message: str):
    chat_id, _ = add_user_message(db, context, message, None)
    add_assistant_message(db, context, chat_id, "Choose a released workflow to run in this chat.")
    save_workflow_context(db, context, chat_id, {}, starting=True)
    return {"chat": chat(db, context, chat_id), "drafts": []}


def save_workflow_context(db, context, chat_id: UUID, selection: dict, *, starting=False):
    if selection:
        context.require("workflows.read", UUID(selection["department_id"]))
        if not any(
            all(option[key] == selection[key] for key in ("department_id", "build_id"))
            for option in released_workflow_catalog(db, context)
        ):
            raise LookupError("That workflow is no longer released in this department")
    with tenant_transaction(db, context):
        stored = db.execute(
            "UPDATE organization_chats SET workflow_context=%s WHERE id=%s AND organization_id=%s "
            "AND owner_user_id=%s AND deleted_at IS NULL AND (%s OR workflow_context IS NOT NULL) "
            "RETURNING id",
            (Jsonb(selection), chat_id, context.organization_id, context.actor.user_id, starting),
        ).fetchone()
    if stored is None:
        raise LookupError("Workflow conversation not found")
    return {"workflow": selection}
