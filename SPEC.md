# Austra blank workspace specification

## Starting state

A new installation starts without business policies, suppliers, purchasing limits, transaction tables, inventory types, or business workflows. The deterministic platform stores administrator access, chats, memory, evidence, draft files, approved files, and audit events under a private workspace directory. It does not create a business database. The previous Stage 1 SQLite and purchasing implementation has been removed from the running application.

The first administrator opens the local chat UI, chooses a passphrase, and enters an Anthropic API key on the private Connections page. The agent then uses Markdown platform skills to learn from administrator chat and signed Slack messages. It asks for missing business facts, drafts files, and saves them under the appropriate workspace folders. An administrator reviews each exact draft before it becomes an approved version.

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
    backend/<name>/v<number>.py
    frontend/<name>/v<number>.js
  records/<approved-schema-name>/<record-id>.json
```

The platform skills shipped in source control describe how to ask questions, author skills and structures, and request credentials. They contain no polysilicon business rule. Business policies, schemas, and workflows appear only after a chat request and review. A record proposal can be approved only when it matches its approved JSON Schema. Records stay as sourced JSON files; there is no business database table. Approved backend and frontend drafts are source files for technical review; the running service never imports or deploys them automatically.

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
4. A proposed business record fails until its user-created schema is approved, then is validated against that schema.
5. Credentials are accepted only through authenticated private setup, never returned in API responses, and never committed.
6. Signed Slack events enter chat once; unsigned events are rejected.
