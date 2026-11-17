# Austra proactive organization platform: implementation spec sheet

Status: proposed implementation plan, 16 November 2026. This specifies future application work; it does not activate a business policy, change a live organization, or deploy a capability.

Source designs: [proactive organizational runtime](proactive-organizational-runtime.md) and [organizational data and retrieval LLD](organizational-data-retrieval-lld.md). The organization's supplied idea brief motivated the semantic model. Existing `src/austra/platform_skills/*/SKILL.md`, `src/austra/skills.py`, tenant migrations, and runtime code are the implementation baseline. Current code and locally revised skills must be re-read immediately before each phase is implemented.

## 1. Product contract and non-negotiable boundaries

Creating an organization opens a guided discovery case. Austra gathers admin text and files, then website and authorized LinkedIn information, asks about missing rules and desired outcomes, shows a sourced summary and examples, and locks an admin-confirmed blueprint candidate. A compiler and build pipeline turn that candidate into workflows, dashboards, or reviewed module code. An independent verifier tests it, collects preview feedback, and cycles defects back to the builder before release. The active deterministic runtime serves chat, UI, API, and integrations from the same blueprint. Post-release business changes create the next blueprint; bug fixes use a new build under the same blueprint.

Architectural invariants:

1. Every read, write, job, cache entry, evidence packet, and external action is scoped to an authenticated organization and permitted department. Existing RBAC remains the access base; business authority adds amount, state, owner, and date conditions. PostgreSQL row-level security (RLS) provides a second barrier. RLS must use a role without `BYPASSRLS`; table owners need `FORCE ROW LEVEL SECURITY` if they query tenant tables. [PostgreSQL RLS](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).
2. An uploaded document, website, LinkedIn export, chat, tool result, or generated claim is evidence. Only an authorized confirmation and compiled blueprint can make business meaning active. Untrusted content cannot grant tool access or change system instructions. [Claude tool-result guidance](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
3. The model may extract, ask, explain, and propose. Typed services authorize, validate, calculate, transition state, call external tools, and audit. No model-authored Python or JavaScript executes in the service from chat.
4. The build is pinned to a blueprint content hash. A production decision is pinned to blueprint, build, semantic revisions, record revisions, actor, and time. Historical answers load those pins, never silently substitute current rules.
5. A skill is a versioned procedure, not permission. The tool gateway and RBAC enforce authority even if a skill, document, or MCP server says otherwise. MCP annotations are hints, not trusted authorization claims. [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
6. An app connection should normally be set up through a curated OAuth sign-in and minimal scopes, with API-key and generic MCP options where appropriate. n8n is a reference for catalog breadth and ease of setup; no n8n adapter or dependency is proposed. MCP does not remove provider consent or scope requirements. [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).
7. A consequential external write requires a concrete reviewed `ActionProposal` unless a confirmed blueprint explicitly authorizes bounded automation. A failed or uncertain write is not retried blindly; reconcile its receipt first.

### Existing foundation and migration seams

| Exists now | Reuse | Required change |
| --- | --- | --- |
| `001_identity.sql` and `002_business_catalog.sql` | Organizations, users, departments, roles, audit, tenant-keyed business catalog | Extend tenant scope into all file and chat operations; add RLS and organization-aware repositories for new tables. |
| `Files` versioned artifacts and review | Preserve legacy history, drafts, and active organization data during migration | Add canonical PostgreSQL revisions and blob-backed evidence; stop full directory scans on request paths after cutover. |
| `skills.py` plus five platform skills | Preserve tested instructions and admin-approved business skills | Replace unconditional concatenation with a short core contract, indexed skill cards, selective loading, and pinned skill versions. |
| `agent.py` planning and read tools | Reuse response validation and bounded read-only worker pattern | Replace broad chat/record context and character-triggered trimming with planned retrieval and token counting on every model call. |
| `mcp_connections.py`, `mcp_gateway.py`, Gmail/Slack | Reuse MCP discovery, schema checks, and existing connections | Curated catalog, OAuth where supported, tenant secret ownership, action classifications, connection health, contract tests. |
| Material planner, formulas, A2UI, reports | Reuse typed calculation and projection patterns for first slice | Bind them to approved capability manifests and one shared authorization/runtime API. |

The current `foundation` skill has important protections around missing facts, complete tables, administrator edits, and no invented verification. Its broad record-management and prompt-loading instructions should be split into focused procedures; do not lose those protections during refactoring. The current `structure_authoring` skill applies explicit rule changes immediately and auto-activates business schemas. New proactive organizations instead confirm a blueprint before behavior becomes active. Define an explicit compatibility path for existing organizations: migrate their active rules into an initial pinned blueprint with an admin review, and keep their legacy operations working until that cutover. The current `credential_setup` skill says all provider actions use MCP; revise it only when a reviewed native API gateway exists. The current `analysis_reporting` skill's complete-set, decimal, demo-data, QC, and shared-stock rules become regression tests.

## 2. Versioned contracts and storage

PostgreSQL is the transactional source for organizations, onboarding cases, evidence metadata, claims, approved semantic graph, blueprints, manifests, records, events, connections, skills, builds, and defects. Private tenant-scoped blob storage holds original files and large extracts. `pgvector` adds embeddings in PostgreSQL for hybrid retrieval; the current `postgres:17-alpine` image needs an extension-enabled replacement at implementation time. Use the existing optional Qdrant service only if measured scale later warrants it. [pgvector documentation](https://github.com/pgvector/pgvector).

| Contract | Required identity and state | Important fields and rule |
| --- | --- | --- |
| `OnboardingCase` | `organization_id`, `case_id`, state | Current question group, owner, sources, unresolved blockers, confirmed blueprint candidate; legal transitions only. |
| `EvidenceSource` / `EvidenceRevision` | Tenant, source ID, immutable revision | Origin, access label, capture time, SHA-256, blob key, locator, extracted status; changed fetch creates a new revision. |
| `EvidenceChunk` / `ChunkEmbedding` | Tenant, source revision, chunk ID, embedding model version | Section/page/table locator, full text vector, token count, content hash, embedding; no approval inferred from similarity. |
| `Claim` | Tenant, source chunk, claim ID | Proposition, confidence, conflict links, `unconfirmed/confirmed/rejected/superseded`, authorized confirmer. |
| `SemanticObject` / `SemanticRevision` / `SemanticEdge` | Tenant, stable key, immutable revision | Capability, Actor, Authority, Policy, Knowledge, Event, Workflow, BusinessObject, Integration, Projection; owner, rationale, valid time, recorded time, sources, typed links. |
| `Blueprint` / `BlueprintMember` | Tenant, immutable candidate or release ID | Version, content hash, exact revision membership, approver, effective time, status, source/acceptance references. |
| `SkillRevision` / `SkillActivation` | Scope, stable skill ID, immutable revision | Name, description, content hash, tests, owner, review state; active platform/organization version pinned to each run. |
| `Build` / `Release` / `Defect` | Tenant, blueprint ID, build ID | Compiler output hash, test results, preview decision, defect loop, deployment and rollback references. |
| `BusinessRecordRevision` / `RuntimeEvent` / `ActionReceipt` | Tenant, object/event ID, immutable revision | Input references, idempotency key, actor, blueprint/build pins, decision, external effect status and receipt. |
| `Connection` / `ActionCatalogEntry` | Tenant, connection ID, action ID | Owner, app, private secret reference, scopes, tool schema hash, read/write class, rate limit, health, revoke state. |

Indexes start with B-tree keys for `(organization_id, kind, stable_key)`, `(organization_id, type, external_id)`, active blueprint membership, `(organization_id, object_id, recorded_at DESC)`, and event time. Use GIN for chunk `tsvector`, selected trigram indexes for names, and tenant-scoped exact vector distance first. Search and version filters are applied before the model receives results. Add HNSW only after measured latency and filtered-recall tests; pgvector notes that approximate index filtering can remove matches after scanning. [PostgreSQL text indexes](https://www.postgresql.org/docs/17/textsearch-indexes.html), [pgvector filtering](https://github.com/pgvector/pgvector).

`blueprint_version` measures business meaning. `build_revision` measures implementation. An unreleased candidate can be edited through new immutable revisions and reconfirmation; published versions remain immutable. Store valid time and recorded time separately. A historical event resolves its pinned blueprint/build first; an unpinned as-of question resolves the effective rule interval and optionally what had been recorded by that date. [PostgreSQL range types](https://www.postgresql.org/docs/17/rangetypes.html).

## 3. Delivery phases and gates

Each phase is independently reviewable. The stated limits and quality thresholds are initial engineering settings and must be tuned on a labeled evaluation set. No phase promotes a business blueprint or application release merely because an agent reports success. Existing data stays available during migration; tests use isolated temporary organizations.

### Phase 0 — foundation, access, and release skeleton

**Build:** Add a tenant-scoped repository interface behind current file and business read paths, transaction-local tenant context, RLS for new tables, append-only audit hooks, a durable job/outbox pattern, and the onboarding/build/release state machine. Create an isolated fixture organization and a backfill inventory for legacy files. Introduce a small skill registry and loader without changing existing skill behavior yet. Set separate `Blueprint` and `Build` IDs and a `Defect` record before any new capability can be released.

**Interfaces:** `TenantContext(actor, organization, departments, support_context)` is derived only from authentication. Repositories accept it explicitly; routes never accept an arbitrary tenant ID as authority. All write jobs carry idempotency and correlation IDs. `POST /api/org/onboarding-cases` is internally triggered by `organization.created`; `GET /api/org/onboarding-cases/current` returns state and next permitted action. Exact endpoint names may follow current route conventions.

**Exit checks:** Cross-tenant and wrong-department reads/writes fail at service and database layers; support access is time-limited and audited. Every state transition rejects illegal predecessors. A defect can return a build from testing/preview to fixing without creating a new blueprint version. Legacy artifact counts and checksums are recorded before migration. Tests cover RLS with the actual non-owner application role, not only mocked filters.

### Phase 1 — evidence intake and source provenance

**Build:** On first org-admin sign-in, show a business and desired-outcome interview. Accept TXT/PDF/DOCX/CSV/XLSX with file size/type validation and resumable jobs. Ask for website and optional LinkedIn Page. Fetch a bounded admin-supplied website scope; LinkedIn uses an approved connection, admin export, or pasted Page text. Upload original bytes, verify hash, then commit revision metadata. Extract pages, headings, table headers, row groups, and locations; OCR only failed pages. Chunk prose initially at roughly 400–800 tokens and preserve whole clauses. Generate full-text vectors and embeddings for changed chunks. Admin sees source previews and can exclude irrelevant material.

**Security:** Restrict URL schemes, public destinations, redirects, DNS resolution, response size, and crawl depth; reject loopback, private networks, metadata endpoints, and redirects into them. Render/parse files as untrusted data and keep originals outside the web root. [OWASP SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). Never present fetched site text as an instruction.

**Exit checks:** A changed website or file produces a new source revision; unchanged bytes avoid re-extraction and re-embedding. Every extracted claim links to a visible page/section/row or URL and capture time. No unsupported LinkedIn access is claimed. An injection string in a PDF or page cannot authorize a tool call. Large spreadsheet rows are imported or aggregated without entering the model context wholesale.

### Phase 2 — clarification, semantic graph, and locked blueprint

**Build:** Extract candidate claims and typed relationships into a model of capabilities, actors, authority, policy, events, workflows, business objects, integration requirements, and projections. Preserve conflicts. Prioritize questions by the consequence of guessing: authority, side effects, source of truth, quantities, ownership, and exceptions first. Ask one focused group at a time, showing source citations and uncertainty. Draft acceptance examples for normal, boundary, and adverse cases. Show an admin-readable behavior summary: data read, decisions, actions, approvers, manual steps, and example outputs. Lock a content-hashed candidate only after authorized confirmation.

**Compiler:** Check references, units, ownership, state transitions, contradictory or overlapping effective rules, RBAC/authority intersection, connection scopes, and acceptance examples. Emit versioned decision tables, event routes, typed mappings, allowed action IDs, and projection descriptors. Refuse a candidate with an execution-critical unknown or conflict. A candidate's source claims and approval receipt remain inspectable.

**Exit checks:** The admin can inspect why each rule exists, amend a question answer, and see exactly what changed before reconfirming. A marketing claim does not silently become a purchasing policy. The compiler rejects overlapping authority limits or an integration action without owner/scope. The locked hash is stable and builds reference it exactly.

### Phase 3 — hybrid retrieval and bounded agent context

**Build:** Route requests as action, current fact, explanation, history, comparison, or discovery. Resolve tenant, actor, department, blueprint, valid time, and source access before retrieval. Exact SQL finds IDs/rules/record revisions. For open-ended evidence questions, search eligible chunks through full-text and vector lanes, up to 30 candidates each; fuse ranks, deduplicate, check authority and coverage, and pack no more than 8 cited passages. Embed approved semantic descriptions for capability discovery, then resolve the selected revision through exact blueprint membership. SQL handles totals and dashboard aggregates.

**Context contract:** Normal target at most 12,000 input tokens; 16,000 hard cap; 2,000–4,000 output reserve. Normal allocation: 2,000 stable instructions/tools, 1,500 current request/recent turns, 2,000 exact semantics/SQL facts, 4,000 evidence, 2,500 tool results/margin. Count the fully assembled prompt on every call. Persist structured case state instead of replaying complete chats. If evidence is incomplete, run a bounded second retrieval or report unknown; do not truncate an operative clause. [Claude token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting), [long-context retrieval research](https://aclanthology.org/2024.tacl-1.9/).

**Exit checks:** Labeled questions include paraphrases, exact IDs, wrong-version traps, conflicting sources, cross-tenant lookalikes, and absent answers. Initial target: at least 95% recall of relevant evidence on answerable cases, zero unauthorized or wrong-version retrievals, and no unsupported numeric claim. Track p50/p95 latency, input/output tokens, citation validity, and cost per resolved task. Compare exact vector search and HNSW only when measured latency needs it.

### Phase 4 — deterministic runtime and first end-to-end release

**Build:** Compile the first isolated SO → inventory availability/reservation → PO draft/approval → email proposal journey. Chat, forms, API, and schedules submit the same typed command. The runtime checks tenant RBAC, dated business authority, current record revisions, and permitted transition. Use decimal arithmetic, units, optimistic concurrency or row locks, unique idempotency keys, and an outbox for side effects. Reuse the current material planner, formulas, and A2UI reports as verified components; do not silently promote generated code. A generated form/dashboard uses projection descriptors. A custom module uses a reviewed source change and the normal PR/CI release path.

**Build and feedback loop:** A capability builder produces a manifest, mappings, fixtures, expected outcomes, and rollback plan pinned to the locked candidate. A verifier runs positive, negative, boundary, permission, retry, and failure scenarios in an isolated organization. An admin previews representative cases. Each failure becomes a defect with expected/observed result, trace, owner, and affected build. Builder fixes → verifier reruns affected and regression cases → admin previews again. The release gate requires the exact confirmed blueprint hash, passing checks, accepted preview, healthy required connections, valid approver, and a rollback plan. Fixes before go-live do not pile up active blueprint versions.

**Exit checks:** Two retries cannot reserve the same stock twice or create duplicate POs/emails. An above-limit PO escalates; a disconnected provider leaves an actionable repair case. A previewed email shows recipient, content, source records, and approval requirement. A historical explanation uses the event-pinned blueprint and build. The same command produces the same decision through chat, UI, and API. A business meaning change after release creates vN+1; a code bug patch retains vN and increments the build/release ID.

### Phase 5 — easy connections and workflow breadth

**Build:** Add a business-language connection catalog with curated MCP and native API templates. Each template declares provider, setup method, OAuth scopes or private key fields, account verification, typed actions, effect classification, event triggers, rate/error behavior, schema hash, and revocation. Prefer platform-owned OAuth registration and a one-click consent flow when the provider supports it; API keys use a private form. Generic remote MCP and reviewed HTTP templates cover providers without a curated integration. Secret values stay in the secret manager, outside chat and agent context. Tool discovery is paginated and schema changes trigger review; provider metadata remains untrusted. The action gateway rechecks tenant, RBAC, business authority, approved workflow, connection health/scope, arguments, rate limit, and idempotency before execution. [MCP tool and authorization specs](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

**Workflow test:** Verify reads against a safe account, map fields with fixture payloads, replay triggers, preview proposed writes, and test expired token, insufficient scope, rate limit, schema drift, timeout, and duplicate webhook delivery. Writes go to a provider sandbox or remain proposals until the release and action gates allow them. Failed/expired connections display a reconnect path and preserve pending work. A provider response is required before claiming an action succeeded.

**Exit checks:** A nontechnical admin can connect a supported provider without pasting credentials into chat, see the exact account and scope, and revoke it. A generic MCP URL works only after server and action-catalog validation. A malicious `readOnlyHint` cannot turn a write into a read. The first full workflow remains operable after reconnect and idempotent replay. Provider-specific OAuth or API limitations appear clearly in setup; they are not hidden by MCP.

### Phase 6 — reusable workflow, dashboard, and ERP capability builder

**Build:** Generalize the first journey into typed capability definitions, event/state machines, decision tables, integration mappings, and projection descriptors. Generate standard forms, tables, queues, charts, and reports from approved definitions with tenant/role filtering. A reviewed custom ERP module is allowed when descriptors cannot express the required interaction; it still calls the same typed runtime and authorization service. Reusable builder templates must include failure paths, idempotency, audit, and acceptance examples. Keep generated UI metadata separate from executable backend code.

**Exit checks:** A second organization with a materially different process can build a workflow without changing the first organization's rules. Dashboard totals reconcile with SQL and filtered records, including demo/live separation and units. Changing a projection cannot weaken policy. A custom code module follows lint, types, tests, PR review, deployment, and rollback; generated code never auto-loads from an uploaded document or chat reply.

### Phase 7 — proactive monitoring, lifecycle, and measured cost

**Build:** Open review cases when evidence changes, a run fails, a policy approaches expiry, a connector schema changes, or observed outcomes conflict with a blueprint. Route each case to the responsible owner with citations and a proposed effect; never silently alter the active blueprint. Maintain cost/latency/retrieval dashboards, source and embedding freshness, connection health, defect escape rate, and rollback signals. Re-embed only changed content or a planned model migration. Prompt caching applies only to stable instructions and tool definitions; cached tokens still count toward the context window. [Claude tool-context guidance](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context), [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

**Exit checks:** A policy expiry creates a review case before an affected action proceeds beyond its valid interval. A failed release can roll back its build while retaining business and audit history. A bug patch does not increment blueprint version. Per-organization metrics show cost per resolved task, retrieval misses, wrong or missing citations, token budget breaches, connection failures, and time to repair. Infrastructure additions require measured need and a documented migration plan.

### Delivery and ownership matrix

Migration numbers below follow the currently committed `001`–`003` sequence; split a migration when review or rollback safety calls for it. Each phase may need several small PRs, but its exit checks are a single gate.

| Phase | Primary feature area and proposed migration | Skill/agent change | Required review evidence |
| --- | --- | --- | --- |
| 0 | `onboarding/`, `build_release/`, tenant repository seam; `004_case_release_foundation.sql` | Core contract and skill registry; preserve old skill loader behind adapter | Real-role RLS, transition/idempotency, legacy counts and rollback tests |
| 1 | `evidence/`; `005_evidence_sources.sql` | Discovery + `intaking-evidence` | File/site provenance, complete locators, SSRF/injection and dedupe tests |
| 2 | `semantics/`; `006_semantic_blueprints.sql` | Semantic designer + elicitation/modeling/authoring skills | Source-to-claim trace, ambiguity interview, compiler rejection, admin confirmation tests |
| 3 | `retrieval/`; `007_search_embeddings.sql` plus pgvector-enabled image | `retrieving-evidence`, `planning-read-only-lookup`, selective skill loader | Labeled lexical/vector recall, wrong-version and tenant exclusion, prompt budget tests |
| 4 | `capabilities/`, `build_release/`; `008_runtime_events.sql` | Builder, verifier, runtime assistant; workflow/report/action skills | Isolated SO/PO/inventory/email replay, feedback loop, release and rollback record |
| 5 | `integrations/`; `009_connections_actions.sql` | `configuring-connections` revised after gateway is real | OAuth/private-secret flow, MCP schema and permission checks, sandbox contract tests |
| 6 | `capabilities/projections.py`, `static/features/`; migration only for needed descriptors | Builder + `building-projections` | Second-tenant workflow, dashboard reconciliation, reviewed custom-module path |
| 7 | `build_release/`, observability hooks; migration only for needed review cases | Supervisor + `triaging-feedback` | Expiry/schema-drift review cases, cost/retrieval dashboard, patch-versus-version tests |

## 4. Agent roles and skill specifications

Use **five logical build-time roles and one constrained runtime assistant**, as in the proactive architecture: Discovery, Semantic designer, Capability builder, Verifier, Supervisor, and Runtime assistant. Instantiate only the roles a case needs; the durable orchestrator owns state and gates. The Supervisor can coordinate read-only specialists but cannot waive confirmation, testing, or release checks. The Verifier must evaluate build output independently. No role receives a database superuser credential or direct provider secret. Their output contracts are structured: `finding + source IDs + uncertainty`, `candidate semantic diff + blocking questions`, `build manifest + fixtures`, `test result + trace + defect`, and `action proposal/answer + citations` respectively.

The current five skills are a valuable baseline. Preserve their tested constraints while converting task-specific instructions into indexed, on-demand skills. Use the [Agent Skills format](https://agentskills.io/specification): a `SKILL.md` with `name` and task/trigger `description`, optional one-level `references/`, and a versioned content hash. The [authoring guidance](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) favors precise descriptions, progressive disclosure, direct references, concrete examples, and verification steps. Austra's current underscore directory names are internal legacy IDs; keep an alias map during migration and use hyphenated names for new portable skills.

| Skill ID and role | Existing source / new behavior | Inputs → output and boundary |
| --- | --- | --- |
| `core-safety` (all) | Split from `foundation`; always-loaded short platform contract | Authenticated actor and task → required tenant/permission/evidence rules; cannot authorize actions. |
| `intaking-evidence` (Discovery) | New; retains `foundation` attachment completeness and provenance | File/URL and access label → source revisions, extraction coverage, locators; untrusted text never becomes instruction. |
| `eliciting-requirements` (Semantic designer) | New; extends focused-question behavior in `foundation` | Claims, conflicts, desired outcome → prioritized questions with cited ambiguity; no invented limits or owners. |
| `modeling-organization` (Semantic designer) | Refines `structure_authoring` | Confirmed claims → typed objects/edges, versions, reason, source IDs; cannot publish a blueprint. |
| `authoring-skills` (Semantic designer / Supervisor) | Refines `skill_authoring` | Reusable method → task trigger, steps, uncertainty, review boundary, eval cases; a skill grants no tool rights. |
| `authoring-workflows` (Capability builder) | Refines `structure_authoring` | Locked manifest → states, transitions, failures, idempotency and fixtures; generated code remains review-only. |
| `building-projections` (Capability builder) | Refines `structure_authoring` and A2UI/report conventions | Typed view descriptor → role-filtered form/table/dashboard; mutation through runtime API only. |
| `configuring-connections` (Capability builder / Runtime) | Refines `credential_setup`; preserve current Gmail/Slack and reconnect behavior | Action catalog, scopes, health → connection proposal or exact tool call; secrets stay private, effect verified. |
| `analyzing-records` (Runtime / Verifier) | Refines `analysis_reporting` and `foundation` complete-list rules | Exact record set and units → reconciled calculation, source IDs, unknowns; no model arithmetic or incomplete totals. |
| `planning-read-only-lookup` (Runtime) | Refines `foundation` compose/saved lookup and recovery lessons | Query intent and schema IDs → bounded SQL plan with coverage flags; no broad record dump. |
| `retrieving-evidence` (Discovery / Runtime) | New from retrieval LLD | Tenant/time/blueprint/query → evidence packet with selected and rejected IDs; no obsolete policy substitution. |
| `verifying-capabilities` (Verifier) | New | Candidate hash, build, fixtures → reproducible pass/fail, traces, defects; verifier cannot mark its own failures waived. |
| `triaging-feedback` (Supervisor) | New | Admin feedback and traces → bug versus business-change classification, owner, next state; no version bump for a pure bug. |
| `reviewing-actions` (Runtime) | New | Typed action proposal, actor, blueprint, connection → visible approval request or bounded automatic authorization; no hidden external write. |

Skill quality contract for **every** platform or organization skill:

- Metadata includes stable ID, version/hash, owner, purpose, precise triggers and exclusions, required tools as documentation, input/output schema, and source/authority level. The server determines available tools independently of metadata.
- Body states preconditions, bounded procedure, uncertainty and failure handling, verification, and handoff. Include one positive example, one missing-data example, and one permission or conflicting-source case. Keep references one level deep and load only the needed section.
- Lint checks metadata, duplicate/overlapping triggers, broken references, secret literals, unsupported tool names, and excessive prompt size. Review checks whether it conflicts with higher-priority platform policy, active blueprint, or current APIs. Organization-authored skills require authorized review and immutable versioning.
- Eval cases cover task routing, unsupported facts, wrong tenant/version, prompt injection, incomplete pagination, numeric reconciliation, provider failure, and action approval. Compare against current skill behavior before activation; pin skill revision in every agent run. A failed skill is repaired and retested before activation, without creating a business blueprint version unless business meaning changes.
- Skills are discoverable by a short catalog card (`id`, description, triggers, owner, version, hash). Load the core contract and only the selected skill bodies and named references. Return a reason and IDs for each selection. Tool definitions also load on demand when supported; stable prefix caching reduces repeated cost but does not reduce context occupancy. [Claude tool-context guidance](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context).

Do not copy every active rule and skill into every request as `load_skills()` currently does. Retrieve the exact active rules for the selected capability and actor; include source/version IDs. User-facing conversations remain brief and in business language, preserving the current `foundation` guidance to state outcomes and ask a focused question when needed.

## 5. Feature ownership and code layout

Keep the Python 3.13/FastAPI backend and existing npm/Jest frontend workflow. Organize new code by business feature, with routes, contract, service, and repository near one another. A feature owns its tables and invariants; cross-feature calls go through a typed public service, not imports into another feature's repository internals. Keep individual files focused, usually below about 300 lines. Existing `web.py`, `files.py`, and `agent.py` are large migration seams; extract behavior only while implementing the corresponding phase, preserve imports through thin adapters, and delete adapters after their callers move. Do not perform a repository-wide rename in one PR.

Proposed target tree (names are a design contract, not files created by this spec):

```text
src/austra/
  app.py                         # FastAPI composition; no business logic
  feature_index.json             # small checked-in path/owner index, CI validated
  platform/                      # existing provisioning and authentication
  tenant/                        # existing RBAC, support context, audit
  onboarding/                    # cases.py, routes.py, service.py, repository.py
  evidence/                      # models.py, routes.py, ingest.py, extract.py, fetch.py, repository.py
  semantics/                     # models.py, compiler.py, versions.py, repository.py
  retrieval/                     # plan.py, exact.py, hybrid.py, pack.py, context.py
  capabilities/                  # contracts.py, authorize.py, runtime.py, events.py, projections.py
  integrations/                  # catalog.py, connections.py, oauth.py, mcp.py, http.py, gateway.py
  build_release/                 # cases.py, compiler_jobs.py, defects.py, releases.py
  agents/                        # orchestrator.py, discovery.py, designer.py, builder.py,
                                 # verifier.py, runtime.py, response_models.py
  skill_catalog/                 # registry.py, selector.py, loader.py, validator.py
  platform_skills/               # existing skill IDs remain during migration;
                                 # new task skills use portable hyphenated directories
  storage/migrations/            # ordered SQL migrations; existing 001–003 stay unchanged
  static/features/               # onboarding/, connections/, workflows/, dashboards/
tests/features/                  # mirrors backend features; isolated tenant fixtures
tests/frontend/                  # feature-oriented Jest tests when frontend build migrates
```

The existing frontend can continue using its current static entry points until the feature-based build is introduced. New frontend code should follow the project's current JavaScript conventions; migrate to TypeScript only with an approved whole-project toolchain change. Do not add a second package manager or test framework. Generated bundles keep the required generated-file header.

### Agent-friendly code index

`feature_index.json` is a small route map, not a second policy source. Each entry has `feature`, `owner`, `public_entrypoints`, `tables`, `events`, `skills`, `tests`, and `legacy_seams`. For example, `retrieval` points to `retrieval/plan.py`, the evidence and semantic repository interfaces, retrieval tests, and the old `agent.py` context builder. CI validates referenced paths, migration ownership, and unique feature IDs. The index is updated in the same PR as a feature change. It contains paths and identifiers only, never credentials, prompt bodies, or business data.

The change-reading procedure for a coding agent is:

1. Read `AGENTS.md`, the requested feature's index entry, and its public contract.
2. Use `rg` inside that feature and its named legacy seams for exact symbols; read only the called files and the relevant tests. Follow one dependency edge when needed.
3. Read only the selected skill card and body for an agent behavior change. Read the active blueprint contract only when changing business execution. Do not load all skills or a whole organization corpus to edit one capability.
4. Change the narrowest feature boundary, add a migration for new storage, update the index, and run that feature's tests plus the required integration/security regressions. Follow the repository's full lint/test and Docker health gate before claiming an application code change is complete.

### Public service contracts

| Service | Minimal method or event contract | Explicit failure |
| --- | --- | --- |
| Onboarding | `open_case(tenant, org_created_event)`; `answer_question(case, actor, answer, source_refs)` | Unauthorized actor, stale case revision, unresolved critical question. |
| Evidence | `capture(source, bytes_or_url, access)`; `read_locators(source_revision, range)` | Unsafe URL, bad file, unavailable authorized content, incomplete extraction. |
| Semantics | `propose_change(case, source_claims)`; `confirm_candidate(actor, candidate_hash)`; `resolve_as_of(time, known_at?)` | Conflict, broken reference, hash mismatch, overlap, missing authority. |
| Retrieval | `plan(query, actor, blueprint, time)` → `EvidencePacket` with IDs, locators, coverage, token count | No authorized support, wrong or missing version, over budget. |
| Runtime | `execute(TypedCommand, ActorContext, idempotency_key)` → decision, event, optional action proposal | RBAC/authority denial, stale revision, invalid transition, unresolved source. |
| Gateway | `verify_connection`; `preview_action`; `execute_approved_action` | Missing scope, expired token, schema drift, uncertain receipt. |
| Build/release | `compile(candidate_hash)`; `record_test`; `record_feedback`; `publish(build_id, approval)` | Unconfirmed candidate, open defect, failed gate, unhealthy required connection. |

All contracts carry typed IDs and structured errors. API routes translate these errors into user-readable language; agents do not infer success from absent exceptions. Events include `organization.created`, `evidence.revision_captured`, `blueprint.candidate_confirmed`, `build.test_failed`, `build.preview_accepted`, `blueprint.released`, `action.proposed`, `action.executed`, and `connection.health_changed`. Event consumers are idempotent and preserve correlation IDs.

## 6. Verification matrix and release policy

| Risk | Required automated test or evidence | Release effect |
| --- | --- | --- |
| Tenant or department leak | Cross-tenant/department repository, search, embedding, cache, blob, and MCP cases under a real app DB role | Block release on any leak. |
| Wrong business version | Current, future, event-pinned historical, as-of valid time, as-known-at recorded time, v1/v2 comparison | Block release on wrong or unlabeled version. |
| Hallucinated or missing fact | Answerable/absent/conflicting questions; citation points to authorized retrieved span; numerical totals reconcile with SQL/decimal | Block affected action; answer unknown when coverage fails. |
| Unbounded context/cost | Count system, tools, skills, messages, evidence, and output reserve on each call; large document batch tests | Reject or decompose over-budget request. |
| Duplicate or uncertain side effect | Retry, timeout, webhook replay, provider success-with-lost-response, outbox replay | Reconcile receipt; no duplicate send/PO/reservation. |
| Skill regression | Old and new skill behavior on complete records, admin updates, demo/QC distinctions, failed connection, missing evidence | Do not activate new skill revision until eval passes. |
| Build feedback | Test failure, admin preview defect, business change, production patch, rollback | Defect loops under same blueprint; only confirmed meaning change increments blueprint. |
| Website/file attack | Private-IP/redirect SSRF, oversized file, malicious tool instruction, unauthorized source | Reject input or quarantine source; no unauthorized action. |

Unit tests cover compilers, version resolution, rank packing, state transitions, and calculations. PostgreSQL integration tests cover actual RLS, indexes, transactions, unique keys, and concurrent retries. Connector contract tests use sandbox/fake providers and captured schemas; no test should mutate live customer data. End-to-end fixtures use at least two organizations and distinct departments. Keep a labeled retrieval evaluation set with source IDs and expected versions, plus a separate skill routing/behavior evaluation set. Run the project's Ruff, pytest, and Jest gates for code changes, then `docker compose up -d --build app` and confirm `docker compose ps app` is healthy as required by this repository.

Release records store the blueprint hash, build artifact, skill revisions, connector schema hashes, tests, preview decision, approver, rollback target, and deployment timestamp. A code fix can produce build 1.0.1 under blueprint v1. A changed approval limit requires blueprint v2 and new acceptance examples. A preview defect stays inside the unreleased candidate/build cycle. A failed deployment rolls the build back without deleting the new candidate, historical events, or admin feedback.

## 7. Cost controls and decision checkpoints

| Decision | Default | Revisit when |
| --- | --- | --- |
| Database/search | One PostgreSQL with B-tree, GIN full text, `pgvector`, private blob store | Query plans or maintenance show sustained pressure; then benchmark partitions or a separate vector service. |
| Vector index | Exact distance over prefiltered tenant/blueprint subset | Labeled p95 latency exceeds the agreed target at real corpus sizes; compare HNSW filtered recall before adoption. |
| Extraction | Deterministic parser and hash dedupe; model only for claims/ambiguity | OCR or model extraction demonstrably improves coverage for a document class. |
| Models | Cheapest evaluated model for bounded extraction; stronger model for ambiguous reconciliation and final review | Quality or cost per resolved task regresses on real cases. |
| Prompts/tools/skills | Short stable core; selected skill bodies, exact rules, deferred tools, cited evidence | Routing misses or extra tool round trips exceed the measured benefit. |
| Caches | Manifest by tenant/blueprint/capability; query embedding and retrieval by tenant/access/version | Cache invalidation, permission changes, or freshness failures appear; prioritize correctness over hit rate. |

Record input/output and cache tokens, embedding calls, search candidates, selected passage tokens, model calls, p50/p95 latency, retrieval misses, source freshness, release defects, and cost per **resolved task**, not merely per model call. Stable prompt prefixes can reduce price but still occupy the context window. Do not commit a fixed model price or provider capability into a skill; those values change. [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [pgvector indexing](https://github.com/pgvector/pgvector).

The first implementation PR should deliver Phase 0's tenant-safe storage seam and release state machine with tests, leaving the current app working. Every subsequent PR should deliver one independently reviewable feature slice and update the feature index, its tests, and the applicable skill evaluation. This minimizes migration risk and keeps the codebase navigable for humans and coding agents.
