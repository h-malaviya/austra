---
name: planning-workflow-solutions
description: Turn an organization-specific brief into a reviewable workflow candidate with visible open decisions; exclude execution and release.
version: 4
owner: platform
review_state: reviewed
source_authority: administrator_answers_and_tenant_evidence
required_tools: []
input: manager_brief_chat_evidence_and_resolved_decisions
output: workflow_steps_outputs_open_decisions_and_acceptance_examples
---
# Planning workflow solutions

Read the manager's brief against the organization's permitted conversation and evidence. Plan the smallest useful workflow for the stated outcome. Define the trigger, ordered steps, human decisions, outputs, exception path, and normal, boundary, and adverse examples in business language. Keep authority, source of truth, units, recipients, and external effects explicit. Suggest an improvement only when it follows from the reported goal or pain point; label it as a proposal until the administrator accepts it.

If a material decision remains unknown or sources conflict, return the exact open decision. Do not fill it with a guess. Save a candidate with open decisions only as an unapproved draft; those decisions block confirmation, build, execution, and release. If a provider connection is unverified, keep that step manual or describe it as a proposed integration; do not claim it is connected. A draft and its examples are not approval to build, execute, or release. The manager decides whether to ask the administrator or submit the candidate; a skill grants no tool rights.

For email replies that update a record, ask for each target field, its source (sender, subject, receipt time, an exact message label, or an exact attachment label), and which record states allow the update. A mapped number needs a label and its approved unit. For a generated email PDF, ask for its title and which record fields it may include. Put both settings in the candidate for explicit admin review and use fictional examples for missing and conflicting values.

## Draft recovery

Keep the outline concise. Supply the editable definition as a single JSON string when the requester has supplied usable rules. Preserve explicit email requirements and open questions on a retry. If the typed definition is incomplete, return the outline for admin review with the unresolved decision. Treat the workflow as saved only after the application confirms it. A saved outline cannot authorize an external action.

**2026-11-24 12:03 IST.** Willow carrier dispatch exposed a provider grammar limit when the planner requested a deeply nested structured schema. Austra now requests a compact outline and validates its editable definition locally. It can retry an invalid definition once and retain a reviewable outline when the detail still needs attention.

## Runtime recovery for tracked Gmail

Plan one tracked conversation per approved workflow email. For supported replies and attachments, the worker saves the Gmail message reference and extracted evidence, then resumes only the interrupted step. Include an outage example where Austra resumes an approved record update from saved evidence, and an interrupted send example where a saved receipt starts tracking without another send. Preserve the approved record, fields, and authority. After repeated read failures or an unconfirmed send, surface the saved work for owner review. Ask for authorization or a business decision only where the workflow needs one.

For automatic supplier replies, confirm the originating record state, expected attachment or new reply text, the literal checks and their findings, the exact outgoing message for each outcome, and any state transition or generated PDF. Mark only the explicitly approved transitions `allow_automatic_email`. The placeholder `[[findings]]` inserts the reviewed finding text for failed checks. Keep automatic replies on the original recipient and Gmail thread. Use a record check for prerequisite internal evidence, such as verified goods receipt. A supplier’s message cannot attest to the buyer’s receipt or authorize payment. Missing or ambiguous source documents require review. A manager’s proposal still needs admin release and explicit approval on the initial email.

For documents whose names and layouts can vary, use `documents` on the rule. Map each role such as `dispatch_notice` to distinctive actual content headings. Attachment checks set `name` to that role. For independent business values use `labels` with approved field aliases and set `contains` to the expected value exactly. Use `comparison: decimal` for counts and amounts, preserving any required currency. A label belongs to the field being read, never to an attachment name. Support natural reply wording through approved `reply_openers` such as “Confirmed” and “I confirm” as standalone statements, followed by separately verified terms. Include `external_id` only in the starting transition's `fields` list. Automatic outcomes must provide exactly the transition's required text fields, exclude internal prerequisites from reply mapping, and include all attachment fields in an earlier authorized record step. Admin sign-off belongs to release, not to open business questions.

Set `expected_field` instead of `contains` when the approved value varies by case. Use a declared internal field, ensure the authorized opening step records it, and do not allow a supplier reply mapping to change it. Automatic record creation is outside the email rule. For tests define one accepted, one mismatching, and one ambiguous example before release.

Set `blocking: false` for readable differences that should reach `on_mismatch` and trigger a correction message or transition. Use `blocking: true` only when a readable difference requires owner review. Missing, conflicting, or unreadable required evidence always requires review even on a non-blocking check.
