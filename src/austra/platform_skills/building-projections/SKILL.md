# Building approved capability projections

Use this skill only after an organization admin confirms a locked blueprint. Read the
active capability manifest, exact tenant scope, and the analysis reporting skill.
This skill describes a build method; it grants no runtime permission.

For each role, choose an approved form, queue, table, chart, dashboard, or report
descriptor. Declare the capability key, record kind, visible fields, eligible states,
roles, and, for numeric views, the decimal measure and unit. A form names an approved
transition. Its action submits a typed command to the shared runtime; the descriptor
never contains executable code or authority rules. A queue is a state-filtered table.

Check complete record counts, source labels, QC status, units, and SQL decimal totals.
Never add demo rows to live totals. Missing or malformed quantities remain unresolved.
Verify that the actor can read the department and that mutations still require the
dated authority rule and current record revision. Test retries, denied roles, stale
records, failure states, and another organization's similarly named records.

If the interaction cannot be expressed by descriptors, create a custom module draft
with a review ticket, a stated interaction, and rollback plan. Implement backend and
frontend source changes through normal PR review, lint, tests, deployment, and rollback.
Keep generated code as a draft; never load it from an upload or chat response. The
reviewed module calls the existing typed command API for every mutation.

Example: a released quality workflow can show received batches in a queue and
verified released weight in a dashboard. If one batch lacks a unit or verified
quantity, mark its aggregate unresolved. If an employee edits a dashboard to expose
a release button, the runtime still denies release without approval authority.
