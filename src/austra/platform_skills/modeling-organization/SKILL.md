---
name: modeling-organization
description: Use after claim review to draft typed organization revisions and relationships for an onboarding candidate; exclude live execution.
version: 1
owner: platform
review_state: reviewed
source_authority: confirmed_claims_only
required_tools: []
input: confirmed_claims_and_admin_answers
output: semantic_revision_diff_and_acceptance_examples
---
# Modeling organization

Create separate Capability, Actor, Authority, Policy, Knowledge, Event, Workflow, BusinessObject, Integration, and Projection revisions only where the evidence supports them. Give every revision a tenant, stable key, owner, rationale, source claim IDs, valid interval, and recorded time. Use typed edges. Preserve superseded revisions and conflicting claims. Draft exact data reads, decisions, actions, approvers, manual steps, outputs, and normal, boundary, and adverse acceptance examples. Run compiler checks and show the diff for administrator review. Never lock or publish a candidate from a model draft. A skill grants no tool rights.

If a confirmed SOP defines a 100 USD manager limit, draft that exact unit and boundary case. If the owner is unknown, leave it unresolved and ask. If two effective rules overlap, keep both revisions visible and block confirmation until the administrator resolves the conflict.
