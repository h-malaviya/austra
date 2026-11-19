CREATE TABLE organization_chats (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    owner_user_id uuid NOT NULL,
    title text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id),
    UNIQUE (organization_id, id, owner_user_id)
);
CREATE INDEX organization_chats_owner_recent ON organization_chats
    (organization_id, owner_user_id, updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE organization_chat_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    chat_id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, chat_id, owner_user_id)
        REFERENCES organization_chats(organization_id, id, owner_user_id)
);
CREATE INDEX organization_chat_messages_order ON organization_chat_messages(chat_id, created_at, id);

CREATE TABLE conversation_memory_notes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    owner_user_id uuid NOT NULL,
    chat_kind text NOT NULL CHECK (chat_kind IN ('onboarding', 'ask')),
    chat_id uuid NOT NULL,
    source_message_id uuid NOT NULL,
    scope text NOT NULL CHECK (scope IN ('chat', 'user')),
    content text NOT NULL CHECK (length(trim(content)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id),
    UNIQUE (organization_id, owner_user_id, chat_kind, chat_id,
            source_message_id, scope, content)
);
CREATE INDEX conversation_memory_user ON conversation_memory_notes
    (organization_id, owner_user_id, scope, created_at DESC);
CREATE INDEX conversation_memory_chat ON conversation_memory_notes
    (organization_id, chat_kind, chat_id, created_at DESC);

CREATE TABLE conversation_memory_summaries (
    organization_id uuid NOT NULL REFERENCES organizations(id),
    owner_user_id uuid NOT NULL,
    chat_kind text NOT NULL CHECK (chat_kind IN ('onboarding', 'ask')),
    chat_id uuid NOT NULL,
    source_message_id uuid NOT NULL,
    content text CHECK (content IS NULL OR length(trim(content)) > 0),
    working_content text NOT NULL DEFAULT '',
    compacted_at timestamptz,
    compacted_message_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, chat_kind, chat_id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id)
);

ALTER TABLE organization_chats ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_chats FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON organization_chats TO austra_tenant_app;
CREATE POLICY organization_chats_owner ON organization_chats TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid)
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid);

ALTER TABLE organization_chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_chat_messages FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON organization_chat_messages TO austra_tenant_app;
CREATE POLICY organization_chat_messages_owner ON organization_chat_messages TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid)
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid);

ALTER TABLE conversation_memory_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_memory_notes FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, DELETE ON conversation_memory_notes TO austra_tenant_app;
CREATE POLICY conversation_memory_notes_scope ON conversation_memory_notes TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid
            OR (scope = 'chat' AND chat_kind = 'onboarding')))
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid);

ALTER TABLE conversation_memory_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_memory_summaries FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_memory_summaries TO austra_tenant_app;
CREATE POLICY conversation_memory_summaries_scope ON conversation_memory_summaries
    TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (chat_kind = 'onboarding' OR owner_user_id =
            nullif(current_setting('austra.actor_id', true), '')::uuid))
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid);
