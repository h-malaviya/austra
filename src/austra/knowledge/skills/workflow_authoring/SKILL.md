---
id: workflow_authoring
version: 1
status: approved
---
# Workflow authoring

Turn a repeated business procedure into a proposed typed state machine. Define states, events, allowed transitions, preconditions, required facts, tool IDs, outputs, retry behavior, escalation points, and completion evidence.

The workflow executor enforces each transition deterministically. An agent may choose or propose a workflow but cannot bypass its checks. A workflow may call only registered tools within their declared permission scope. Missing or conflicting facts lead to a question or manager review.

Test normal, boundary, duplicate-event, and failure cases. Publish only after review with a version, affected-case analysis, and rollback path. Do not turn a one-time chat answer into a reusable workflow without review.
