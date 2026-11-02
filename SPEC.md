# Austra blank workspace specification

## Starting state

A new installation starts without business policies, suppliers, purchasing limits, transaction tables, inventory types, or business workflows. The deterministic platform stores administrator access, chats, memory, evidence, draft files, approved files, and audit events under a private workspace directory. It does not create a business database. The previous Stage 1 SQLite and purchasing implementation has been removed from the running application.

The first administrator opens the local chat UI, chooses a passphrase, and enters an Anthropic API key on the private Connections page. The agent then uses Markdown platform skills to learn from administrator chat and signed Slack messages. It asks for missing business facts, drafts files, and saves them under the appropriate workspace folders. Business record and schema drafts from chat are validated and activated automatically; other draft types require administrator review.

## Folder contract

```text
data/blank/
  admin.json
  .env
  chats/
  memory/short_term/
  audit/
  drafts/
  evidence/
  approved/
    policy/<name>/v<number>.md
    skill/<name>/v<number>.md
    workflow/<name>/v<number>.md
    schema/<name>/v<number>.json
    formula/<name>/v<number>.json
    backend/<name>/v<number>.py
    frontend/<name>/v<number>.js
  records/<approved-schema-name>/<record-id>.json
```

The platform skills shipped in source control describe how to ask questions, author skills and structures, and request credentials. They contain no polysilicon business rule. Business policies and workflows appear only after a chat request and review. A chat record save creates a minimal schema from supplied fields when none exists, then validates the record without separate administrator review. Records stay as sourced JSON files; there is no business database table. Approved backend and frontend drafts are source files for technical review; the running service never imports or deploys them automatically.

## Chat and connector behavior

- Multiple chats persist as JSON files. Replies, draft versions, and their source chat remain visible.
- Deleting a chat removes it from the chat list while retaining its short-term memory record and approved files as long-term memory. Recent retained memory is included in later agent context.
- The agent can search and read approved files, platform skills, chat evidence, and recent records through bounded typed tools. Tool calls create an audit entry without storing their arguments.
- The agent uses Claude Sonnet 5 when a key is configured. Without it, the chat is saved and Connections explains where to get a key.
- The agent may request Gmail or Slack connections when the requested capability needs them. Gmail uses Google OAuth consent after the administrator saves the OAuth client ID and secret; its refresh token and mailbox address are obtained by the server and stored in the ignored, private `.env` file. Values are never returned by the API or placed in chat.
- Slack receives direct messages and mentions through Socket Mode with an app-level token, and can also accept signed HTTP events. It groups replies by Slack thread and sends them through the bot token. Gmail credentials can be stored, but Gmail sending is not yet implemented in this blank substrate.
- Up to five PDF, DOCX, or CSV attachments can be selected together, previewed as removable chips, and sent with one message and one agent analysis. Evidence bytes are stored by SHA-256 and reusing the same file in a chat does not add another attachment message. Uploaded material is evidence, not an instruction with authority.
- The app runs with `docker compose up -d --build app` at `http://127.0.0.1:8000` by default (`AUSTRA_PORT` can override the host port). Its workspace is bind-mounted at `data/blank`; the HTTP port is published on host loopback only. Redis, MongoDB, and Qdrant are optional Compose profile services. They remain stopped until an infrastructure draft is approved. Stop requests use the same review path. Docker images and named volumes remain after stopping a service.
- The authenticated audit API and UI expose actor, action, target, source chat, draft, outcome, timestamp, and loaded skills for agent proposals and administrator reviews. Credential values and raw tool arguments are excluded. Approved lifecycle changes use the Docker socket inside the app container.

## Boundaries

The LLM writes drafts and chooses which files to propose. Typed platform code controls identity, file paths, supported artifact types, publication, and connector access. Approval of an infrastructure draft can start or stop only the three optional Compose services. Approval of a business draft does not approve a purchase order, release production, transfer money, change the running backend, or migrate a database. Those capabilities require code, tests, a technical release, and a separately approved business authority model.

This is a local prototype. Before network deployment, add individual accounts and roles, durable session storage, a production secret manager, backups, isolation for generated code, connector OAuth setup, and a reviewed release pipeline. The local `.env` satisfies private prototype setup; production secrets must use a dedicated secret manager.

## Acceptance checks

1. A fresh workspace contains no business definitions or database.
2. A chat can create proposed skills, policies, workflows, schemas, and code files.
3. Reviewing a draft publishes a versioned file in the correct folder without executing it.
4. A chat record with no schema creates one automatically and saves without a review step; records with an existing schema must validate against it.
5. Credentials are accepted only through authenticated private setup, never returned in API responses, and never committed.
6. Signed Slack events enter chat once; unsigned events are rejected.

## Stock, orders, and material analysis (phased implementation)

### Problem and evidence

In the 28 October 2026 chat “Stock vs open orders check,” Austra repeatedly reported
getting only the first inventory page, then produced partial tables and deferred the
same request across multiple replies. Its final reply treated per-tonne ratios as a
barrier to an order-wise raw-material split. They can be applied per order: allocate
available finished goods once, then multiply each order's production gap by the
applicable per-tonne ratios and the active
wastage multiplier. The saved finished-goods records also contain demo and verified
copies of the same lot IDs; summing records without origin checks doubles stock.

### Phase 1 evidence audit (2 November 2026)

The reproduction source is the saved 16-message chat at
`data/blank/chats/121ba93ede4d460aac7b5e4aafb2d192.json`. The following
observations describe what the chat shows; they do not treat Austra's earlier
answers as authoritative business data.

| ID | Observed behavior | Evidence in the chat | Requirement established |
| --- | --- | --- | --- |
| E1 | Austra reported only 8 of 11 finished-goods lots and deferred the coverage verdict. | Assistant reply at 2026-10-28 10:40 UTC. | A numeric coverage verdict requires an explicit complete record count and distinct lot IDs. |
| E2 | After the administrator requested order-wise raw needs and current inventory, Austra reported only 9 of 17 raw lots. | User at 10:55 UTC; assistant at 10:55 UTC. | Retrieval must finish before the answer, or the answer must mark specific quantities unknown. |
| E3 | One reply was literally `placeholder`; another showed a material-needs table without material quantities. | Assistant replies at 10:52 and 10:56 UTC. | Reject empty, placeholder, and partially populated final reports. |
| E4 | The last reply listed only three material requirements and said per-tonne ratios prevented an order-wise split. | Assistant reply at 11:07 UTC. | Apply each applicable ratio to each order's production gap after stating one finished-stock allocation method. |
| E5 | The administrator requested charts and graphs, then had to ask repeatedly for the missing detail. | User replies at 10:46, 10:52, 10:55, and 11:07 UTC. | One reply must contain the verdict, order-wise detail, inventory, caveats, and a navigable visual view. |

The current saved-record snapshot contains two `customer_sales_order_request`
records, one `sales_order_so_aus_2026_203` record, 17 raw-material lots, and six
material-ratio records. It contains 22 finished-goods records but only 11 distinct
lot IDs: each ID has one normal and one `synthetic_demo` copy. This is a source
classification problem even if pagination works correctly. These counts are
reproduction metadata, not a fresh business stock report. At run time, the current
non-demo records supply quantities and statuses, the active approved policy supplies
the allowance, and the saved ratio records supply per-tonne inputs. Prior assistant
messages are evidence of the failure, never the source of a new numeric claim.

The transcript confirms repeated partial answers. It does **not** preserve the
read-tool arguments or a complete call trace, so the exact cause of the reported
page repetition remains unproven. It also does not establish that the displayed
three-material arithmetic was wrong; the confirmed failures are incomplete
coverage, omitted order-wise calculations, and an incorrect explanation of why
per-order quantities could not be produced. The allocation choice is a separate
assumption and must be visible in the report.

### Phase 1 acceptance baseline

Phase 1 is complete when this spec provides: (a) a transcript-linked failure
ledger; (b) source counts and duplicate classification; (c) a distinction between
observed failures and unproven root causes; (d) a deterministic calculation and
presentation contract; and (e) a test matrix with isolated fixture cases for
complete data, partial pages, demo duplicates, missing quality evidence, mixed
kg/t ratios, unknown material mapping, and rendering fallback. Later phases
implement these checks;
Phase 1 does not modify live records, approved business rules, or the saved chat.

| Case | Isolated fixture or failure injection | Required result | Phase |
| --- | --- | --- | --- |
| T1 | Three open orders, eligible finished stock, and six ratios | Each order's production and material rows reconcile with totals. | 2 |
| T2 | A repeated page or a `complete=false` result | Recover the remaining records or refuse a complete verdict; never reuse the first page as the remainder. | 2 |
| T3 | A demo copy for every real finished-goods lot | Count each real lot once and disclose excluded demo records. | 2 |
| T4 | Missing verification or quality status, or conflicting active lot IDs | Stop the affected numeric plan with a specific source issue. | 2 |
| T5 | Ratios mixing tonnes and kilograms, including hydrogen | Convert to one mass unit before multiplying and retain exact decimal values. | 2 |
| T6 | A ratio with no matching raw inventory material | Show calculated need; show availability and gap as unknown. | 2 |
| T7 | The A2UI renderer is unavailable or a table exceeds the visual row limit | Keep the full answer readable in the same chat message. | 3 |
| T8 | A follow-up such as “give me the remaining, order-wise” | Keep the original request's scope and return the full order-wise plan once. | 2–3 |
| T9 | An empty or placeholder model answer | Reject it before presenting a final report. | 2–3 |

### Contract

1. A data-bearing request receives one complete answer with a concise verdict, an
   interactive visual summary, order-wise figures, material-wise figures, current raw
   inventory, calculation basis, and explicit unknowns. Text remains available to
   assistive technology and integrations that cannot render A2UI.
2. Planning reads a complete snapshot of the relevant record schemas. Each distinct
   order and lot is counted once. Demo records never augment non-demo inventory.
   Conflicting non-demo duplicates or absent required quantities stop numeric claims.
3. An order's eligible finished stock requires matching grade and known quality,
   released status, compatible purity and form/size, and a nonnegative available
   quantity bounded by on-hand less reservations and holds. Allocation is shown per
   order and is explicitly hypothetical; it does not reserve stock.
4. Default allocation is earliest requested delivery first, then order ID. This is a
   presentation assumption, not an approved operational priority. A request with an
   explicit allocation method uses that method if supported; otherwise the answer
   asks for the missing choice before making affected claims.
5. Raw-material requirement per order is `max(order kg - allocated finished kg, 0)
   / 1000 * ratio per tonne * active allowance`. Conversion to kilograms happens
   before summing, using decimal arithmetic. Raw inventory availability excludes
   holds and reservations. Material names and units must match the active ratio and
   inventory records; unmapped materials are shown as unknown, never as zero.
6. Every number in the rendered plan comes from deterministic code, with source
   record IDs, rule version, formula, and an as-of date available for inspection.
   Ratios with an “ideal reaction equivalent” basis are labeled as such; no production
   timing, purchase approval, or feasibility is inferred from mass balance alone.
7. General arithmetic tools accept bounded declarative operations and decimal values;
   arbitrary model-authored code is never executed in the running service. Reusable
   read plans and approved skills remain versioned, with code changes released through
   the normal development workflow.

### Input and output model

| Input | Required fields | Validation |
| --- | --- | --- |
| Open order | order ID, status, ordered kg, grade, minimum purity, size range, requested delivery | Exclude historical and closed orders; reject a duplicate active order ID or missing mass. |
| Finished lot | lot ID, grade, purity, form/size, on-hand kg, reserved kg, QC-hold kg, available kg, QC and verification status | Prefer non-demo source; exclude demo, held, unverified, and incompatible lots. Reported available kg cannot exceed on-hand less reserved and held. |
| Material ratio | material/stage, unit, quantity per tonne of the applicable finished grade, basis | Convert tonnes to kg before multiplying; reject an unsupported unit or grade mapping. |
| Raw lot | lot ID, material identity, on-hand kg, reserved kg, QC-hold kg, available kg, QA and verification status | Aggregate released, verified, non-demo lots by material. Missing material mapping is unknown, not zero. |
| Active allowance | approved policy version and numeric multiplier | Apply once after the theoretical ratio calculation; never alter saved ratios. |

The plan payload contains one row per order with demand, allocated finished mass,
remaining production mass, and source lot IDs. It contains one row per ratio with
the per-tonne value, each order's requirement, total requirement, available raw mass,
gap, ratio record ID, inventory lot IDs, and basis. It also contains all current raw
inventory materials, including those not represented by the approved ratio. The
complete answer is stored as message text; the A2UI payload is a presentation of
the same calculation and attaches to that message ID.

### Calculation and reconciliation example

For an isolated test fixture with three solar orders of 10 t, 14 t, and 18 t, plus
1.5 t eligible finished goods, earliest-delivery allocation yields production gaps
of 8.5 t, 14 t, and 18 t. Total production gap is 40.5 t. With a quartzite ratio of
2.89 t per finished tonne and an approved multiplier of 1.05, total quartzite need
is `40.5 × 2.89 × 1.05 = 122.89725 t`. If eligible quartzite inventory is 80 t,
the mass gap is 42.89725 t. The fixture must also reconcile the six individual
material ratios, including kilogram-based hydrogen, without treating process gases
or intermediate-stage quantities as interchangeable.

These invariants are checked before presenting a numeric verdict:

- Every order's allocated finished mass is between zero and its demand.
- The sum of allocated mass across orders does not exceed eligible stock.
- Each order's production mass equals demand less allocated mass.
- Each material's total need equals the sum of its order-wise needs.
- Each material's gap is `max(need - eligible raw inventory, 0)` only when matching
  raw inventory is known; otherwise the gap is unknown.
- Totals use exact decimal values. UI formatting never changes stored arithmetic.

### Presentation and failure behavior

An A2UI report uses tabs for overview, orders, materials, current inventory, and
method. Proportional bars provide a quick visual comparison. Every bar has a text
label and value so color or geometry is not the only way to read it. The complete
Markdown answer remains stored and is sent to Slack or other text-only channels.
The app displays it if A2UI cannot render or if the visual omits rows; a complete
visual hides the duplicate source table. Ordinary table replies receive the same
A2UI treatment without modifying the underlying numbers.

The overview stacks finished stock and remaining production against each order's
demand. The materials view compares needed and available mass on a common scale;
unknown availability is labeled unknown, never drawn as zero. Activating a chart
row exposes its exact values. Numeric Markdown tables use a single-series
chart. Report content uses explicit light-theme contrast and compact spacing within
chat bubbles, including when the operating system prefers a dark theme.

Tabular answers render as native tables with the reply's actual columns, including
future customer, supplier, and other record fields. A single table does not need a
tab bar; multiple tables use tabs. When an available numeric column exists, the
chart defaults to that measure and displays each value beside its bar. Hovering or
focusing a bar opens a field list for that row; click also works on touch devices,
and Escape dismisses the popup. Larger replies show the 20 largest chart values
while retaining up to 200 rows in the scrollable table and all rows in the text
fallback. Structured order, material, and inventory reports use the same table
component for their detailed views.
Explanatory text after a table stays visible in the A2UI answer. A following
list of item-and-description entries becomes a detail table, while its caveats
remain readable below it.

The service does not turn a missing or conflicting source into a numeric zero. It
states which source or mapping prevents a complete result. A material with no
matching raw lot may still have a calculated need, while its availability and gap
remain unknown. An unsupported purity grade or conflicting active lot blocks the
affected plan. A chart-rendering failure shows the complete text answer.

### Phases and acceptance

- **Phase 1 — evidence and spec (implemented):** document the failure modes and source-of-truth
  rules above. Preserve the live business workspace.
- **Phase 2 — calculation (implemented for the saved solar/6N model):** add a read-only decimal planning tool, duplicate and
  completeness checks, allocation, unit conversion, and tests in temporary workspaces.
  A three-order, six-ratio case must reconcile order gaps and material totals exactly.
- **Phase 3 — presentation (implemented):** return one A2UI surface attached to the answer with
  interactive sections and proportional bars, plus a complete text fallback. The
  surface must display the same computed values and never hide unknowns.
- **Phase 4 — generalization (implemented):** an administrator reviews a versioned
  `formula` JSON draft before it becomes executable. It declares a bounded decimal
  expression and maps each named input to an approved schema, identity field, and
  numeric value field. `run_approved_formula` reads one identity from a complete
  record snapshot, excludes demo copies, rejects missing or conflicting active
  sources, and returns the result with its formula version, source record IDs,
  source values, and any read-only scenario overrides. The tool never executes
  generated code or changes source records. Tests cover baseline and scenario
  results, duplicate sources, missing records, invalid mappings, and unsafe
  expressions. Future mappings require a new reviewed formula version or code review.
