---
name: triaging-feedback
description: Classify a failed release or changed observed outcome against an approved blueprint.
version: 1
owner: platform
review_state: reviewed
source_authority: active_blueprint_and_pinned_trace
required_tools: []
input: expected_observed_blueprint_and_trace
output: classification_owner_citations_and_next_step
---
# Triaging feedback

Read the active blueprint revision, release build, and pinned event trace. Record the
expected and observed outcome with source IDs. Ask the responsible owner to decide
whether the implementation violated unchanged business meaning or the intended
business meaning has changed. Keep uncertain feedback open for clarification.

An implementation bug gets a patch build under the same blueprint. Require independent
verification and admin preview before release. A business meaning change opens a new
clarification case, then requires source-backed confirmation of the next blueprint,
build, verification, and preview. Never edit or silently republish an active rule.
