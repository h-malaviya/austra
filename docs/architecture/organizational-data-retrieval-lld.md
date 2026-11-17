# Organizational data and retrieval: low level design

Status: researched proposal, 16 November 2026. This is a design for Austra's proactive organization model; it is not an active business policy or an application change.

## Design decision

Use PostgreSQL for tenant scoped metadata, approved semantics, business records, revisions, full text search, and vector embeddings through `pgvector`. Store original PDF, DOCX, TXT, and other large evidence bytes in a tenant scoped content addressed blob store. Begin with exact lookup, hybrid full text and vector retrieval, and narrow graph traversal. The optional Redis, MongoDB, and Qdrant services are not needed for this first implementation. Austra's current `postgres:17-alpine` image requires an extension enabled replacement before this design can run; that is an implementation change, not part of this proposal.

The three data classes have different authority and retrieval paths:

| Class | Examples | Store and read path |
| --- | --- | --- |
| Evidence | Uploaded SOP, website capture, LinkedIn export, email, admin answer | Immutable source revision in blob storage; metadata and searchable passages in PostgreSQL. Evidence can support a claim but is not an approved rule. |
| Organizational semantics | Capability, actor, authority, policy, event, workflow, relationship, reason | Typed, approved, time aware revisions in PostgreSQL; exact lookup by organization, blueprint, kind, key, and effective time. |
| Operational state | SO, PO, inventory lot, approval, email proposal and receipt | Tenant scoped current record plus append only revisions/events. SQL computes joins and aggregates; the model does not reconstruct totals from raw rows. |

This keeps semantic meaning as first class, queryable data while allowing JSON bodies for uncommon business fields. Frequently filtered fields such as order ID, status, department, effective time, and external reference remain typed columns with indexes. The canonical, approved semantic revision is the rule source; extracted document chunks are supporting evidence.

## 1. Physical storage and ownership

Every table and blob key includes `organization_id`. Department owned rows also carry `department_id`. The application resolves both from the authenticated actor and resource before a query. PostgreSQL row level security is a second barrier, using a transaction local tenant setting and a database role that cannot bypass RLS. Search, caches, jobs, connectors, and audit entries carry the same tenant scope. A source's access label is checked before its chunks can be retrieved.

| Table or store | Core fields | Purpose |
| --- | --- | --- |
| `evidence_sources` | `id, organization_id, department_id, kind, uri, title, captured_at, owner_id, access_label` | One attachment or external source and its provenance. |
| `evidence_revisions` | `id, organization_id, source_id, sha256, blob_key, extracted_text_key, recorded_at, status, supersedes_id` | Immutable captured content. Identical bytes can share a blob while attachments keep separate provenance. |
| `evidence_chunks` | `id, organization_id, revision_id, ordinal, locator, text, token_count, search_vector` | Searchable text with page, section, row, or URL location. |
| `chunk_embeddings` | `organization_id, chunk_id, embedding_model, content_sha256, embedding, created_at` | One embedding per chunk and model version, indexed only after access scoped exact search becomes too slow. Re-embed only changed chunks or a planned model migration. |
| `semantic_revision_embeddings` | `organization_id, semantic_revision_id, embedding_model, content_sha256, embedding` | Searchable descriptions of approved capabilities and other semantic objects. Blueprint membership still determines which revision applies. |
| `claims` | `id, organization_id, proposition, evidence_chunk_id, extracted_at, status, confidence` | Candidate facts, conflicts, and admin confirmations. A model generated claim is never silently approved. |
| `semantic_objects` | `id, organization_id, department_id, kind, stable_key` | Stable identity for policy, authority, capability, event, workflow, and related concepts. |
| `semantic_revisions` | `id, object_id, organization_id, payload, valid_from, valid_to, recorded_at, source_claim_ids, rationale, owner_id` | Immutable meaning, including effective time and why it changed. |
| `semantic_edges` | `organization_id, blueprint_id, from_revision_id, relation, to_revision_id` | Typed links such as `governs`, `authorizes`, `triggered_by`, and `supported_by`. |
| `blueprints` and `blueprint_members` | `organization_id, version, status, content_hash, confirmed_by, published_at, effective_at, semantic_revision_id` | An exact, immutable set of approved semantic revisions. Candidate edits have separate draft revisions. |
| `business_records` and `business_record_revisions` | `organization_id, department_id, type, external_id, current_revision_id, payload, occurred_at, recorded_at` | Current operational state and history. Use typed columns or projections for common filters. |
| `runtime_events` and `action_receipts` | `organization_id, actor_id, blueprint_id, build_id, event_type, correlation_id, idempotency_key, occurred_at, outcome` | Replay, audit, retries, and proof of external effects. |
| Blob storage | `organization_id/content_sha256` | Original bytes, extracted full text, and large generated reports. Keep the blob private and reference it by ID. |

The first deployment can use an S3 compatible object store or a private local blob directory with the same interface. Production uses durable backups and encryption. Database metadata and the blob must be committed in a recoverable order: upload blob, verify its checksum, then commit the source revision; orphan blobs are collected later.

### Indexes to create first

| Query | Index |
| --- | --- |
| Exact object or order | B-tree unique `(organization_id, kind, stable_key)` and `(organization_id, type, external_id)` |
| Current or historical meaning | B-tree `(organization_id, object_id, recorded_at DESC)`; `(organization_id, kind, valid_from DESC)`; unique `(organization_id, version)` on blueprints |
| Blueprint membership and semantic links | B-tree `(blueprint_id, semantic_revision_id)`; `(organization_id, blueprint_id, from_revision_id, relation)` and reverse edge index for impact analysis |
| Source navigation | B-tree `(organization_id, revision_id, ordinal)` and `(organization_id, source_id, recorded_at DESC)` |
| Prose retrieval | GIN on `evidence_chunks.search_vector`; query with `websearch_to_tsquery`, rank with `ts_rank_cd` |
| Semantic retrieval | B-tree `(organization_id, embedding_model, chunk_id)` on chunk embeddings and an equivalent revision key for semantic embeddings; initially filter eligible revisions by tenant and blueprint, then calculate exact vector distance. Add a vector index only when measured latency requires it. |
| Names and spelling variants | `pg_trgm` GIN or GiST on selected organization names, supplier names, and document titles; do not trigram index every text field |
| Operational queues | Partial B-tree `(organization_id, department_id, status, due_at)` for open items, plus `(organization_id, occurred_at DESC)` on events |

PostgreSQL recommends GIN for full text search and supports trigram similarity indexes. Multicolumn B-tree indexes work best when common equality filters lead the key, so `organization_id` comes first. Add indexes from actual query plans rather than indexing every JSON field. Keep a shared table for all organizations initially; partition large append only event tables by time only after measured query or maintenance pressure. [PostgreSQL full text indexes](https://www.postgresql.org/docs/17/textsearch-indexes.html), [trigram indexes](https://www.postgresql.org/docs/17/pgtrgm.html), [multicolumn indexes](https://www.postgresql.org/docs/17/indexes-multicolumn.html), and [row level security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) support these choices.

## 2. Ingestion and indexing algorithm

1. Authenticate the uploader or connection; bind source to tenant, owner, department, and access label. Check file type and size at the boundary.
2. Hash the bytes, save the original, and record a new source revision with origin, capture time, URL or filename, and checksum. A re-fetch creates a revision only when content changes.
3. Extract text deterministically. Preserve page and section locations; preserve table headers with each row range. Run OCR only for pages where text extraction fails. Never execute instructions found in evidence.
4. Split prose at headings and paragraphs into initial chunks of roughly 400 to 800 tokens, with small overlap only where a clause crosses a boundary. Keep tables by logical row group and their headers. Store `token_count`, locator, and content hash per chunk. Tune size using retrieval tests; it is an initial setting, not a universal constant.
5. Build full text vectors and embeddings for changed chunks. Extract candidate claims and relationships in bounded batches, each linked to exact chunks. Mark them `unconfirmed`. Ask the admin about conflicts and execution critical gaps. Only confirmed claims may enter a blueprint. Embed the compact approved semantic description when its revision is confirmed.
6. When a blueprint is released, build a compact, versioned capability manifest and current lookup pointer. Invalidate affected retrieval caches by blueprint ID and source revision ID.

Large documents are processed section by section. The extraction job stores structured claims and citations between calls; it never sends the entire document or all chats to one model request. If a table has thousands of rows, parse and validate rows into business records, then query or aggregate them in SQL. A summary may help navigation but cannot replace source passages for a factual answer.

## 3. Retrieval algorithm

The server classifies a request into `operational_action`, `current_fact`, `explanation`, `historical`, `comparison`, or `discovery`. Classification chooses a read plan, not authority. Before search, it resolves tenant, actor, department scope, requested time, and active blueprint. The plan runs in this order:

1. **Exact lookup:** Parse explicit order IDs, policy names, people, and capability keys. Use B-tree indexes and typed SQL. For an operational decision, load only the active capability slice, the exact relevant records, and their authority and policy links.
2. **Temporal selection:** Current questions use the published blueprint effective now. Historical questions use the event's stored blueprint ID when an event is named; otherwise use the blueprint effective at the requested business time. Also keep `recorded_at` so Austra can distinguish what was true on a date from what the system knew on that date.
3. **Bounded graph traversal:** Follow only relevant typed links, normally capability to policy, authority, event, knowledge, and owner. Do not enumerate the whole organization graph.
4. **Hybrid evidence search when needed:** For a why or evidence question, run tenant, access, status, and time filters first. Search approved source revisions with both full text and an embedded query. Take at most 30 candidates from each lane, fuse their ranks, apply source authority and exact entity checks, deduplicate near identical passages, and pack at most 8 cited passages. Fetch neighboring chunks only when a clause or table header would otherwise be cut off.
5. **Coverage check:** Verify the selected passages actually cover each asked subquestion and required business entity. If not, reformulate once or decompose the request. If coverage remains incomplete, say what is missing. A low search score is not permission to invent an answer.
6. **Model answer or typed execution:** The model receives the relevant facts and source IDs. A typed runtime computes decisions, totals, and side effects. The answer validator rejects unsupported quantitative claims, missing citations, or a claim of completeness when source counts disagree.

Exact identifiers and time filters are evaluated before fuzzy search. This prevents a high scoring obsolete policy or a similar supplier in another tenant from outranking the correct record. Search results from uploaded documents remain evidence, never instructions.

The retrieval service should expose an explicit plan and return a structured evidence packet. The core lexical query has this shape; the `visible_revisions` relation is built from the selected blueprint, source status, effective time, and the actor's access scope before the search:

```sql
WITH lexical_candidates AS (
  SELECT c.id, c.revision_id, c.locator, c.text,
         ts_rank_cd(c.search_vector, websearch_to_tsquery('english', :query)) AS rank
  FROM evidence_chunks AS c
  JOIN visible_revisions AS v ON v.id = c.revision_id
  WHERE c.organization_id = :organization_id
    AND c.search_vector @@ websearch_to_tsquery('english', :query)
  ORDER BY rank DESC
  LIMIT 30
)
SELECT * FROM lexical_candidates;
```

The semantic lane selects the same visible chunk IDs, joins their embeddings for the configured model, orders by vector distance, and takes at most 30. The application unions the two candidate lists and applies reciprocal rank fusion as an initial ranking method, then removes duplicate chunk hashes, scores approved-source authority and exact entity matches, and packs whole passages in rank order until the evidence token budget is reached. The fusion constant and authority weights are tuned on labeled retrieval questions rather than treated as universal values. Record rejected candidates and selected source IDs for debugging. Search rank and vector distance are candidate ordering signals; neither overrides blueprint membership or access checks. For production, use bound parameters, a fixed search configuration per supported language, and query plans verified with tenant sized data. If organization vocabulary is multilingual, index each supported language with its matching text search configuration; use `simple` only where linguistic stemming is unsuitable.

### Fetch limits by request

| Request | Initial fetch into application | Maximum sent to model |
| --- | --- | --- |
| Operational action or numeric report | Exact capability manifest, applicable policy and authority revisions, SQL selected records or aggregate | Usually 0 document chunks; compact facts and decision trace, target 2,500 tokens |
| Current policy or business explanation | 1 to 3 exact semantic objects, linked rationale, 4 to 8 cited chunks | Evidence target 5,000 tokens, hard 8 chunks |
| Historical or audit answer | Event's exact blueprint/build ID, 1 to 2 relevant semantic revisions, 2 to 4 source passages | Target 5,000 tokens; never mix with current policy without labeling both |
| Broad discovery or onboarding | Process one section or 8 to 12 chunks per extraction job; persist structured results | One bounded batch at a time; no whole organization dump |
| Dashboard or many records | SQL aggregate and paginated UI rows | Summary and exceptions only; raw rows stay outside model context |

The numbers above are starting budgets and must be measured against Austra's own questions. A user asking for a complete long document receives a multi-stage extraction with a coverage report, not a false one-call summary. For a complex question, the server can make multiple small retrieval calls and merge supported results.

### Vector search and its scaling boundary

Embedding is part of initial document ingestion and semantic approval. Embed source chunks and approved semantic descriptions once per content hash and model version; embed each query once for the vector lane. For an open ended request such as “how do we handle a supplier who delivers only part of an order?”, the vector lane can surface a passage headed “partial receipt and backorder” even when the query shares few words with it. It can also find an approved capability described in different words. Keep a labeled test set of paraphrases, exact IDs, historical dates, and confusing near matches. Vector similarity is discovery only: the exact blueprint, effective revision, and permission checks still decide what can be used.

Start with **exact** vector distance over the eligible tenant and revision subset. The existing B-tree filters can make this economical for small organization corpora. Measure query latency and recall as chunk counts grow. If exact search misses the latency target, test an HNSW index and measure recall with tenant and status filters; approximate indexes may return too few allowed matches because filtering occurs after the index scan. Use iterative scans or tenant aware physical separation if necessary. Do not assume a global HNSW index is safe or effective for many small tenants. [pgvector's project documentation](https://github.com/pgvector/pgvector) documents exact search, HNSW filtering behavior, and hybrid search with PostgreSQL full text search.

## 4. Version retrieval rules

| User intent | Versions loaded |
| --- | --- |
| Normal current work | Only the active published blueprint and effective semantic revisions; exclude drafts and superseded versions. |
| Scheduled future behavior | The confirmed future effective revision, clearly labeled as future, plus current revision if comparison is requested. |
| “Why was this approved last March?” | The runtime event's pinned blueprint, build, and record revisions; related source evidence and rationale. Do not substitute today's rule. |
| “What was the rule on 1 March?” | Revision whose `valid_from <= date < valid_to`; optionally constrain `recorded_at` to what was known then. |
| “What changed between v1 and v2?” | Exactly the two blueprint member sets, their changed semantic revisions, and cited reasons. |
| Bug report | Same business blueprint plus old and patched build IDs and execution traces. A code patch does not increment the organization blueprint version. |

All decisions record `blueprint_id`, `semantic_revision_ids`, `business_record_revision_ids`, `build_id`, and `occurred_at`. This makes past explanations exact. A temporal query must distinguish **valid time** (when the business rule applies) from **recorded time** (when Austra learned or approved it). PostgreSQL `tstzrange` can represent validity intervals; the compiler rejects overlapping contradictory effective rules in one blueprint. [PostgreSQL range documentation](https://www.postgresql.org/docs/17/rangetypes.html) covers interval containment and overlap constraints.

## 5. Context management and hallucination controls

Set a product budget below the model's maximum context window. For a normal answer, target at most **12,000 input tokens** and enforce a **16,000 input token hard cap**, with **2,000 to 4,000 output tokens reserved**. A normal input allocation is: 2,000 stable instructions and tool schemas, 1,500 current request plus recent turns, 2,000 exact semantics and SQL facts, 4,000 retrieved evidence, and 2,500 tool results and margin. The extra 4,000 tokens up to the hard cap accommodate unusually complex but still bounded requests. Count the fully assembled request with the provider's token counting API before every model call, including system prompt, tools, images, and documents. These are configuration defaults to tune through evaluation, not the model's capacity.

Context is assembled as: stable platform instructions; short current task; exact approved model slice; ranked cited evidence; current user request and unanswered questions. Keep only a short rolling conversation window and a structured case state of confirmed facts, unresolved questions, decisions, and source IDs. Do not treat a generated chat summary as an approved policy. Drop low ranked evidence when over budget. Never truncate an operative clause, numerical table, or relevant source mid-sentence; retrieve it in another bounded call or ask for clarification. Do not include all approved rules, all record schemas, or all previous chats by default.

Each answer separates `supported_fact`, `inference`, and `unknown`, and carries source or revision IDs for consequential claims. The server checks citations point to authorized retrieved evidence and validates numbers against typed calculations. If two approved sources conflict, the affected action stops and opens a clarification case. A model can still err, so maintain regression questions with expected source IDs and decision outcomes. Long context alone is not a reliability strategy: controlled research found that relevant information can be used less reliably when buried among many passages. [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) and [Claude token counting documentation](https://platform.claude.com/docs/en/build-with-claude/token-counting) motivate the bounded, measured approach.

## 6. Cost and performance controls

- Use one PostgreSQL service with `pgvector` and blob storage. Do not start a separate vector database, cache cluster, or search service before measurements justify them.
- Embed only new or changed chunks and cache normalized query embeddings within the correct tenant and model scope. Record embedding calls and storage as separate costs.
- Deduplicate source bytes and chunks by hash within a tenant. Re-extract only changed revisions. Use deterministic parsers and SQL before any model call; OCR only pages that need it.
- Route simple classification and candidate extraction to a cheaper model after quality tests; use the stronger model for ambiguity, semantic reconciliation, and final review. The runtime's policy evaluation has no model cost.
- Cache compiled capability manifests by `(organization_id, blueprint_id, capability_id)` and invalidate on publication. Cache retrieval candidates by tenant, access scope, source revision set, query, and time; never reuse a result after permission or source changes.
- Place stable platform instructions before dynamic evidence so provider prompt caching can reuse the prefix. Cache reads still occupy context tokens. Track cached and uncached usage separately.
- Log per request input/output tokens, retrieval candidates, evidence tokens actually used, latency, cache hits, model calls, citation coverage, and cost. Review unanswered or corrected answers as retrieval failures.

As an illustration at the [16 November 2026 Claude Sonnet 5 list rates](https://platform.claude.com/docs/en/about-claude/pricing), a 12,000 input / 2,000 output token answer costs about **$0.044** in text model usage before caching, tools, or infrastructure. Sending 60,000 input tokens for the same 2,000 output tokens costs about **$0.14**. That is why the evidence budget matters. The actual provider rate and prompt composition must be checked at deployment; the design does not depend on these exact prices. [Claude prompt caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) supports caching a stable prefix, while its context documentation confirms cached tokens still occupy the context window.

The release gate uses a tenant scoped evaluation set: exact policy recall, historical version correctness, numerical reconciliation, unauthorized-source exclusion, citation accuracy, response latency, and cost per resolved task. Start with an initial target of at least 95% retrieval recall on answerable test cases and zero wrong-version or cross-tenant retrievals; revise thresholds against actual business risk. Use `EXPLAIN (ANALYZE, BUFFERS)` on representative queries and add an index or partition only when a measured bottleneck appears.

## 7. Migration from the current prototype

The present `Files.records()` and `Files.approved()` scan JSON files, `search_workspace` uses case-insensitive substring matching, and the agent includes recent chat and workspace summaries before a character-based threshold triggers token counting. This design replaces those paths behind a tenant scoped repository and retrieval service. Preserve existing files as migration evidence; write canonical rows and source revisions with checksums, compare counts and hashes, then switch reads per organization. Do not use live business data for development tests.

Implement in this order: (1) tenant scoped source and semantic tables; (2) exact active/as-of revision queries and RLS; (3) chunk extraction, GIN search, embeddings, and hybrid rank fusion; (4) bounded context assembler with real token counting and citation checks; (5) retrieval evaluation and index tuning. This order makes the first SO/PO/inventory/email capability reliable without requiring every possible organization feature up front.
