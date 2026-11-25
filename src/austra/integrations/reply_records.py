"""Write scheduled replies to the record and field set approved at release."""

from psycopg.types.json import Jsonb

from austra.capabilities.reply_mapping import CONTENT_SOURCES, TRACKED_EMAIL_EVENT, mapped_value


def apply_record_replies(db, org, department, job, replies, added, evidence, event):
    binding = db.execute(
        "SELECT e.payload,a.manifest FROM outbox_jobs j "
        "JOIN runtime_events e ON (e.organization_id,e.department_id,e.id)="
        "(j.organization_id,j.department_id,j.event_id) "
        "LEFT JOIN releases r ON r.organization_id=e.organization_id AND r.state='active' "
        "AND r.build_id=(e.payload->>'build_id')::uuid "
        "LEFT JOIN blueprints b ON (b.organization_id,b.id)=(r.organization_id,r.blueprint_id) "
        "LEFT JOIN capability_build_artifacts a ON (a.organization_id,a.build_id,a.blueprint_hash)="
        "(r.organization_id,r.build_id,b.content_hash) "
        "WHERE j.organization_id=%s AND j.department_id=%s AND j.id=%s AND e.event_type=%s",
        (org, department, job, TRACKED_EMAIL_EVENT),
    ).fetchone()
    if not binding:
        return None
    target, manifest = binding
    ids = {item["message_id"] for item in (*added, *evidence)}
    if not manifest:
        return {message: {"reason": "The approved workflow build is no longer active."} for message in ids}
    current = db.execute(
        "SELECT kind,state,data FROM capability_records WHERE organization_id=%s AND department_id=%s "
        "AND id=%s FOR UPDATE", (org, department, target["record_id"]),
    ).fetchone()
    if not current:
        return {message: {"reason": "The linked record is unavailable."} for message in ids}
    kind, state, data = current
    definition = next((item for item in manifest["capabilities"]
                       if item["record_kind"] == kind and item["key"] == data.get("capability_key")
                       and (not item["department_ids"] or str(department) in item["department_ids"])), {})
    mapping = definition.get("reply_mapping")
    if not mapping:
        return {}
    if state not in mapping["states"]:
        return {message: {"reason": "The record is outside the approved states for reply updates."}
                for message in ids}
    contents = dict(db.execute(
        "SELECT message_id,content FROM workflow_reply_evidence WHERE organization_id=%s "
        "AND department_id=%s AND job_id=%s AND message_id=ANY(%s)",
        (org, department, job, list(ids)),
    ).fetchall())
    cursors = dict(data.get("_reply_sources", {}))
    units = dict(data.get("units", {}))
    fields = {field["key"]: field for field in definition["fields"]}
    changes = {}
    outcomes = {}
    for reply in replies:
        message = reply["message_id"]
        if message not in ids:
            continue
        cursor = [reply["received_ms"], message]
        updated, review, waiting = [], [], []
        for spec in mapping["fields"]:
            field = spec["target"]
            if cursor <= cursors.get(field, []):
                continue
            if spec["source"] in CONTENT_SOURCES and message not in contents:
                waiting.append(field)
                continue
            value = mapped_value(spec, fields[field], reply, contents.get(message))
            if value is None:
                review.append(field)
                continue
            cursors[field] = cursor
            changes[field] = value
            if fields[field]["type"] == "decimal":
                units[field] = fields[field]["unit"]
            updated.append(field)
        outcomes[message] = {"updated": updated, "review": review, "waiting": waiting}
    if changes:
        data.update(changes)
        data["units"] = units
        data["_reply_sources"] = cursors
        revision = db.execute(
            "UPDATE capability_records SET data=%s,revision=revision+1,updated_at=now() "
            "WHERE organization_id=%s AND department_id=%s AND id=%s RETURNING revision",
            (Jsonb(data), org, department, target["record_id"]),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO capability_record_revisions (organization_id,department_id,record_id,"
            "revision,state,data,event_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (org, department, target["record_id"], revision, state, Jsonb(data), event),
        )
    return outcomes
