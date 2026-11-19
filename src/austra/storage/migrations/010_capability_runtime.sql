CREATE TABLE capability_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('sales_order', 'stock_lot', 'purchase_order', 'email_proposal')),
    external_id text NOT NULL CHECK (length(trim(external_id)) > 0),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    state text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, department_id, kind, external_id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id)
);
CREATE TABLE capability_record_revisions (
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    record_id uuid NOT NULL,
    revision integer NOT NULL,
    state text NOT NULL,
    data jsonb NOT NULL,
    event_id uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, record_id, revision),
    FOREIGN KEY (organization_id, record_id) REFERENCES capability_records(organization_id, id),
    FOREIGN KEY (organization_id, event_id) REFERENCES runtime_events(organization_id, id)
);
CREATE TABLE capability_command_results (
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    idempotency_key text NOT NULL,
    command_hash text NOT NULL CHECK (command_hash ~ '^[0-9a-f]{64}$'),
    event_id uuid NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (organization_id, department_id, idempotency_key),
    FOREIGN KEY (organization_id, event_id) REFERENCES runtime_events(organization_id, id)
);
CREATE TABLE capability_build_artifacts (
    organization_id uuid NOT NULL,
    build_id uuid NOT NULL,
    blueprint_hash text NOT NULL CHECK (blueprint_hash ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL,
    fixtures jsonb NOT NULL,
    rollback_plan text NOT NULL CHECK (length(trim(rollback_plan)) > 0),
    verification jsonb,
    preview_state text NOT NULL DEFAULT 'pending' CHECK (preview_state IN ('pending', 'failed', 'accepted')),
    preview_by uuid,
    preview_at timestamptz,
    PRIMARY KEY (organization_id, build_id),
    FOREIGN KEY (organization_id, build_id) REFERENCES builds(organization_id, id),
    FOREIGN KEY (organization_id, preview_by) REFERENCES users(organization_id, id)
);
CREATE TABLE capability_action_receipts (
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    job_id uuid NOT NULL,
    provider_key text NOT NULL,
    provider_receipt text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, job_id),
    FOREIGN KEY (organization_id, job_id) REFERENCES outbox_jobs(organization_id, id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id)
);
CREATE TABLE capability_connection_health (
    organization_id uuid NOT NULL REFERENCES organizations(id),
    provider_key text NOT NULL,
    state text NOT NULL CHECK (state IN ('healthy', 'disconnected')),
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, provider_key)
);
ALTER TABLE defects ADD COLUMN trace jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE TRIGGER capability_record_history_immutable BEFORE UPDATE OR DELETE
    ON capability_record_revisions FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER capability_receipts_immutable BEFORE UPDATE OR DELETE
    ON capability_action_receipts FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER capability_commands_immutable BEFORE UPDATE OR DELETE
    ON capability_command_results FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['capability_records', 'capability_record_revisions',
        'capability_command_results', 'capability_build_artifacts',
        'capability_action_receipts', 'capability_connection_health'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO austra_tenant_app', name);
        IF name IN ('capability_build_artifacts', 'capability_connection_health') THEN
            EXECUTE format('CREATE POLICY runtime_read ON %I FOR SELECT TO austra_tenant_app '
                || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid)', name);
            EXECUTE format('CREATE POLICY runtime_insert ON %I FOR INSERT TO austra_tenant_app '
                || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'')', name);
            EXECUTE format('CREATE POLICY runtime_update ON %I FOR UPDATE TO austra_tenant_app '
                || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'') '
                || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'')', name);
        ELSE
            EXECUTE format('CREATE POLICY runtime_read ON %I FOR SELECT TO austra_tenant_app '
                || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND (current_setting(''austra.all_departments'', true) = ''on'' OR '
                || 'department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))', name);
            EXECUTE format('CREATE POLICY runtime_insert ON %I FOR INSERT TO austra_tenant_app '
                || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' AND '
                || '(current_setting(''austra.all_departments'', true) = ''on'' OR '
                || 'department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))', name);
            EXECUTE format('CREATE POLICY runtime_update ON %I FOR UPDATE TO austra_tenant_app '
                || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' AND '
                || '(current_setting(''austra.all_departments'', true) = ''on'' OR '
                || 'department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '','')))) '
                || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' AND '
                || '(current_setting(''austra.all_departments'', true) = ''on'' OR '
                || 'department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))', name);
        END IF;
    END LOOP;
END $$;
