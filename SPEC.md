# Stage 1 specification and delivery plan

## Purpose and boundary

Austra begins with no business records. A request in the UI, Slack, or email opens a case. An agent using Claude Sonnet 5 interprets the request, consults approved policies and recorded facts, and proposes the next actions and any missing business fields. The application verifies authority before an action changes records or communicates externally. Stage 1 covers purchasing, sales orders, inventory, and stock for the Ahmedabad business.

Stage 1 does not control furnaces, reactors, safety systems, production scheduling, product release, payment, or accounting. Historical Excel import is deferred. A sales quantity of finished polysilicon cannot be compared directly with a quantity of quartz without an approved conversion model.

## Operating rules

1. Every request creates a case with a unique ID, source, requester, timestamp, and evidence.
2. Source messages and attachments are data. They cannot change agent instructions, permissions, or policies.
3. The agent may choose the sequence of approved tools and propose new record types, fields, or statuses. It cannot run arbitrary SQL, deploy code, or silently change the meaning of existing records.
4. A manager may approve a decision for one case or publish a versioned rule for future cases. Conflicting or missing policies trigger review.
5. A PO may be approved and sent automatically only when a verified supplier, complete commercial terms, and manager-configured limits all pass. The CEO's quantity and unit-price caps are recorded, but no supplier is approved yet, so the current live-send limit is effectively zero.
6. Every proposed and completed action records the facts, source references, policy version, actor, timestamp, and result. Retries must not duplicate POs or external messages.
7. Transaction facts live in the database. Original PDFs and messages live in protected evidence storage; a permission-aware index helps retrieval. The index is never the source of truth.

## Target agent architecture

This system follows the separation used by coding agents such as Claude Code: a reasoning loop chooses among tools, reusable Markdown skills supply task-specific knowledge, persistent memory supplies relevant context, and permissions constrain execution. Austra is a business operations product, so its tool contracts, approval limits, audit trail, and release process are specific to this plant.

| Layer | Responsibility | Source of truth |
| --- | --- | --- |
| Deterministic backend | Typed APIs and tools for case, supplier, quote, PO, sales, stock, email, and Slack operations; input validation, authorization, idempotency, and audit | Python code and database constraints |
| Agent runtime | Selects a relevant skill, retrieves facts, plans tool calls, handles uncertainty, and explains decisions | Active model plus bounded context |
| Skills | Reusable task instructions and workflows, including a skill-authoring skill | Versioned `SKILL.md` files |
| Business policies | Commercial limits, approval conditions, and exceptions | Versioned approved Markdown and policy records |
| Memory | Case history, past decisions, approved knowledge, and useful working notes | Database and indexed documents with provenance |
| System builder | Proposes changes to frontend, backend, database, tool contracts, and design | Reviewed code and migrations |

The agent may choose which existing tool to call and in what sequence. The tool itself has deterministic behavior and a typed input/output contract. A Markdown file can guide an agent but cannot grant access, change an API contract, or override an execution guard. Frequently repeated procedures become skills; facts such as a supplier quote remain records with a source and verification state.

Reusable operational workflows will be typed state machines: states, events, transitions, required facts, permitted tool IDs, retry keys, and escalation outcomes. A deterministic executor will enforce them. A companion Markdown file explains the business intent and examples; the prose alone is not executable authority. The agent may propose a new workflow version when a query reveals a missing path. Publication requires validation and review.

### Skills, memory, and self-extension

Built-in skills live in `src/austra/knowledge/skills/<name>/SKILL.md` with an ID, version, and approval state. The case-planning skill is loaded into the current Sonnet planning call. The agent may retrieve approved skill-authoring, workflow-authoring, and system-builder guidance through a typed read tool. It may propose a new or updated Markdown skill in a chat; manager approval publishes an immutable numbered `.md` version and retains its hash and source case. Published skills become available to the agent on future cases. The initial PO guidance is stored as Markdown and seeded into the versioned policy store.

A skill-creation request follows this path: capture the request as a case; identify the existing skill or missing capability; draft a Markdown change; review its exact diff, tool scope, and effect on previous cases; approve and publish a new version. The current publisher records versions and manager decisions. Automated skill evaluation, rollback UI, and technical review are still needed before production use. Updating a skill does not itself add a backend tool or permission. A requested new tool, workflow engine step, UI change, class, or database migration enters a separate typed system-change proposal. Manager approval records the business decision; implementation still needs isolated development, tests, technical review, staging, and deployment. The business agent cannot patch a live service while processing an order.

Memory is separated by authority: approved policies and skills are instructions; verified business records support transactions; uploaded files and emails are unverified evidence; case history and agent notes help retrieval but cannot promote themselves to rules. Retrieval must return source IDs and versions. Long-term memory can suggest a new rule or skill, then the same review path decides whether it becomes active.

The backend uses Python classes and Pydantic types for cases, tool inputs, outputs, and proposal validation. Target domain types include `Case`, `Supplier`, `PurchaseRequirement`, `PurchaseOrder`, `SalesOrder`, `InventoryLot`, `StockMovement`, `PolicyVersion`, `SkillVersion`, `WorkflowVersion`, `ToolInvocation`, and `Approval`. Frontend contracts should use TypeScript types when the UI grows beyond this prototype. Schema changes need versioned migrations; the agent may propose fields without directly rewriting tables.

## Required business records

- Case, message, evidence, event, proposed action, approval, and policy version.
- Supplier, material or product item, quotation, purchase order, and email thread.
- Customer, sales order, inventory lot, stock movement, and reservation.
- Proposed record definition and field definition, with version and review state.

Each material or product records its unit, grade, location, source, and verification status. Available stock is derived from verified quantity and reservations rather than a free-form number supplied by the agent.

## Authority and escalation

The manager configures PO value limits, currencies, eligible suppliers and materials, allowed price variance, payment terms, and sender identities. New suppliers, bank-detail changes, missing terms, uncertain stock, conflicting policies, and out-of-limit commitments require manager review. A manager decision must distinguish a one-time exception from a reusable policy change.

The initial CEO-provided policy sets a maximum of 3 metric tonnes for one purchase requirement and USD 7,000 per metric tonne, implying a material value of at most USD 21,000. Splitting a requirement across POs does not avoid the limit. It deliberately leaves the approved supplier list, grade, freight and tax treatment, delivery terms, payment terms, and unusual price changes unresolved. The agent must send cases with those missing or conflicting facts to a manager. This policy alone cannot authorize any live PO because no suppliers are approved yet.

The application enforces permissions at execution time. Input and output filters are additional controls, not the sole guardrail. Credentials remain in the deployment secret store and are never written into business records or source control.

## Stage 1 delivery sequence

1. **Foundation:** case intake, event log, policies, proposed actions, and manager access control.
2. **Agent planning:** Sonnet 5 loads the relevant approved Markdown skill, reads a bounded set of case facts and approved policies, and returns typed action and record proposals.
3. **Business records:** supplier, PO, sales, inventory, stock movement, and reservation tools with validation and duplicate protection.
4. **Communication:** verified email and Slack connectors, thread tracking, document capture, and explicit send gates.
5. **Manager loop:** review queue, one-time decisions, versioned policy publication, and case replay against new policies.
6. **Skill and builder loop:** propose, test, review, publish, and roll back Markdown skills; separately develop and release new tools, workflows, UI, and schema changes.
7. **Pilot:** compare agent proposals with manager decisions before enabling low-risk external actions, then expand only within approved limits.

## External service setup

Google Workspace Gmail uses a dedicated business mailbox and OAuth credentials with a scope that permits both send and thread reads, such as `gmail.modify`. An approved PO creates a reviewed email outbox item. A manager sends it from the chat; the Gmail message and thread IDs are stored. A manager can check that thread for replies, which enter the same case once by Gmail message ID. A send timeout or crash can leave the outcome uncertain; the system will not retry it without reconciliation. Polling or Gmail push notifications are still needed for automatic reply monitoring. The Slack integration uses a dedicated bot, verifies signed requests, and identifies events so retries do not duplicate cases. Slack PDF retrieval and bot replies require the bot's authorized scopes and credentials.

The current UI and business API accept requests only from the same computer; the signed Slack endpoint can receive remote events. Before live use, replace the local manager token with individual identities and roles. Enter the PO approval limits, allowed suppliers, sender mailbox, and escalation owners. Until then, external sends and PO approval remain disabled.

## Acceptance checks

- A first request creates a case and proposed records without inventing missing facts.
- A repeated external event does not create another case, PO, or email.
- No PO is approved or sent before limits are configured and validated.
- A sales shortage with comparable verified units is escalated according to the active policy; unlike materials are not compared.
- Each decision shows its source facts and policy version, and a manager can correct it.
- A new policy applies only after manager approval and leaves earlier decisions auditable.

## Current implementation slice

The current implementation has multiple persistent chats, follow-up messages, attachments, and manager review cards. The backend has signed Slack text intake, a case and audit store, policy drafting and activation, and Sonnet 5 analysis with bounded typed read tools. A versioned Markdown workflow enforces current `new` and `review` case transitions. The agent may propose record definitions, policy drafts, Markdown skills, sourced memory, and typed system changes. A manager can review these in chat. Approved policies become new active versions, approved skills are versioned on disk, and only approved memory is retrievable. System-change approval does not modify running code.

Purchasing now has typed supplier proposals and PO drafts. Manager approval verifies a supplier for a material. The deterministic PO gate requires a verified supplier, matching material, specified grade and payment, delivery, freight, and tax terms, no more than 3 metric tonnes, no more than USD 7,000 per tonne, and no more than USD 21,000 of material value. It also checks combined approved POs for the same case and material against quantity and value limits. These checks cover internal approval only; no PO is sent. A new supplier extracted from a file is still unverified until a manager checks and approves it.

Stock and sales have typed proposals too. A manager can verify a measured receipt with item, raw or finished classification, lot, grade, location, and metric tonne quantity. The stock view derives available finished stock from approved receipts less approved sales reservations. Sales approval requires sufficient verified finished stock for the same item; raw quartz cannot satisfy a polysilicon sale. This does not yet schedule production or model conversion yield.

Manager-approved record definitions can be filled through guided chat prompts; entered values remain unverified. An admin can attach PDF, DOCX, and CSV files to a chat; extracted text is indexed, previewable, and supplied to analysis as unverified reference material. Image-only PDFs need text review or OCR. The Gmail send and thread-read code is implemented behind manager review and mailbox credentials, but no live Gmail call has been made. Automatic mailbox monitoring, Slack attachments and bot replies, automatic analysis of Slack cases, workflow publication, system-builder release tooling, and individual manager roles are still needed. The local file store and shared manager token are prototype controls, not production identity or encrypted storage.

## Architecture references

- [Claude Code extension layers](https://code.claude.com/docs/en/features-overview)
- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code memory](https://code.claude.com/docs/en/memory)
- [Claude Code permissions](https://code.claude.com/docs/en/permissions)

## Run the local prototype

Use `uv sync --extra dev` to install the Python environment. Set `ANTHROPIC_API_KEY` and `AUSTRA_MANAGER_TOKEN` through the local environment or a secret manager, then run `uv run uvicorn austra.web:app --host 127.0.0.1 --port 8000`. Open `http://127.0.0.1:8000`. The database defaults to `data/austra.db`; `AUSTRA_DB_PATH` and `AUSTRA_DOCUMENT_DIR` can change storage locations. Gmail send and reply checks require `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, and `GMAIL_SENDER_EMAIL` for a dedicated authorized mailbox. The Slack endpoint additionally needs `SLACK_SIGNING_SECRET` and, when restricting to one workspace, `SLACK_ALLOWED_TEAM_ID`. A public HTTPS endpoint and Slack app configuration are required for Slack to deliver events.
