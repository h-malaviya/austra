CREATE TABLE integration_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    owner_user_id uuid NOT NULL,
    template_key text NOT NULL,
    endpoint_url text,
    account_label text,
    granted_scopes text[] NOT NULL DEFAULT '{}',
    action_catalog jsonb NOT NULL DEFAULT '[]'::jsonb,
    catalog_hash text,
    health text NOT NULL DEFAULT 'setup' CHECK (health IN
        ('setup', 'healthy', 'repair', 'schema_review', 'revoked')),
    repair_reason text,
    checked_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id)
);
CREATE TABLE integration_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    build_id uuid NOT NULL,
    action_name text NOT NULL,
    action_schema_hash text NOT NULL CHECK (action_schema_hash ~ '^[0-9a-f]{64}$'),
    effect text NOT NULL CHECK (effect IN ('read', 'write')),
    fixture jsonb NOT NULL,
    trigger_fixture jsonb NOT NULL,
    mapping jsonb NOT NULL DEFAULT '{}'::jsonb,
    write_preview jsonb,
    approved_by uuid NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, department_id, connection_id, blueprint_id, action_name),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, connection_id) REFERENCES integration_connections(organization_id, id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id),
    FOREIGN KEY (organization_id, build_id) REFERENCES builds(organization_id, id),
    FOREIGN KEY (organization_id, approved_by) REFERENCES users(organization_id, id)
);
CREATE TABLE integration_executions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    proposal_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    idempotency_key text NOT NULL,
    argument_hash text NOT NULL CHECK (argument_hash ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('reserved', 'succeeded', 'uncertain', 'repair')),
    receipt text,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, department_id, idempotency_key),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, connection_id) REFERENCES integration_connections(organization_id, id),
    FOREIGN KEY (organization_id, approval_id) REFERENCES integration_approvals(organization_id, id),
    FOREIGN KEY (organization_id, actor_user_id) REFERENCES users(organization_id, id)
);
CREATE TABLE integration_action_proposals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    argument_hash text NOT NULL CHECK (argument_hash ~ '^[0-9a-f]{64}$'),
    arguments jsonb NOT NULL,
    preview jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '15 minutes'),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, approval_id) REFERENCES integration_approvals(organization_id, id),
    FOREIGN KEY (organization_id, actor_user_id) REFERENCES users(organization_id, id)
);
ALTER TABLE integration_executions ADD CONSTRAINT integration_execution_proposal_scope
    FOREIGN KEY (organization_id, proposal_id)
    REFERENCES integration_action_proposals(organization_id, id);
CREATE TABLE integration_webhook_deliveries (
    organization_id uuid NOT NULL,
    connection_id uuid NOT NULL,
    provider_event_id text NOT NULL,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, connection_id, provider_event_id),
    FOREIGN KEY (organization_id, connection_id) REFERENCES integration_connections(organization_id, id)
);
DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['integration_connections', 'integration_approvals',
        'integration_executions', 'integration_action_proposals',
        'integration_webhook_deliveries'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO austra_tenant_app', name);
        EXECUTE format('CREATE POLICY scoped_read ON %I FOR SELECT TO austra_tenant_app '
            || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid)', name);
        EXECUTE format('CREATE POLICY scoped_insert ON %I FOR INSERT TO austra_tenant_app '
            || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.can_write'', true) = ''on'')', name);
        EXECUTE format('CREATE POLICY scoped_update ON %I FOR UPDATE TO austra_tenant_app '
            || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.can_write'', true) = ''on'') '
            || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.can_write'', true) = ''on'')', name);
    END LOOP;
END $$;
