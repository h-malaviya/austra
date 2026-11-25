"""Stable identities for unresolved discovery decisions."""

import re


def decision_key(question: str) -> str:
    words = set(re.findall(r"[a-z]+", question.casefold()))
    if words & {"change", "scope", "request"}:
        if words & {"truth", "authoritative", "record"} and words & {
            "system", "tool", "tracker", "email", "source"
        }:
            return "change_request_source_of_truth"
        if "account" in words and "owner" in words and words & {
            "approve", "approval", "threshold", "backup", "escalation", "sign"
        }:
            return "change_request_commercial_authority"
        if "signed" in words and "order" in words:
            return "change_request_signed_order"
    if words & {"review", "reviewer", "reviews"} and words & {"client", "message", "sending"}:
        return "client_message_reviewer"
    if "outage" in words and words & {
        "approver", "approves", "approve", "approval", "authority", "authorized"
    }:
        return "outage_statement_approver"
    if "forwarded" in words and words & {"note", "override"} and words & {
        "approval", "approvals", "approved", "authenticity", "sent", "sender"
    }:
        return "forwarded_note_authenticity"
    if "logs" in words and words & {"sensitive", "client", "regulated"}:
        return "sensitive_log_handling"
    if words & {"triage", "severity", "incident"} and words & {
        "criteria", "threshold", "urgent", "routine", "major"
    }:
        return "incident_severity_threshold"
    if "spreadsheet" in words and words & {"truth", "record", "permanent"}:
        return "intake_system_of_record"
    if "engineer" in words and words & {
        "available", "unavailable", "assign", "assigned", "backup", "escalate", "escalation"
    }:
        return "engineer_unavailable_escalation"
    if "investigation" in words and words & {"findings", "documentation", "documented"}:
        return "investigation_handoff_record"
    if words & {"connections", "connectors", "integrations"} and words & {
        "manual", "provider", "tool", "tools", "confirm"
    }:
        return "manual_integration_boundary"
    normalized = re.sub(r"\([^)]*\)", "", question.casefold())
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def unique_decisions(questions: list[str], resolved: list[str] | None = None) -> list[str]:
    resolved_keys = {decision_key(item) for item in resolved or []}
    seen = set(resolved_keys)
    unique = []
    for question in questions:
        if not isinstance(question, str) or not question.strip():
            continue
        key = decision_key(question)
        if key not in seen:
            unique.append(question.strip())
            seen.add(key)
    return unique
