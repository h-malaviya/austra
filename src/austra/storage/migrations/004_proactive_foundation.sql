DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'austra_tenant_app') THEN
        CREATE ROLE austra_tenant_app NOLOGIN NOBYPASSRLS;
    END IF;
END $$;
GRANT austra_tenant_app TO CURRENT_USER;
DO $$ BEGIN
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO austra_tenant_app', current_schema());
END $$;

CREATE TABLE onboarding_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    created_event_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'created' CHECK (state IN
        ('created', 'collecting', 'clarifying', 'ready_for_review', 'locked_candidate')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    blueprint_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, created_event_id)
);

CREATE TABLE blueprints (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    blueprint_version integer NOT NULL CHECK (blueprint_version > 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'candidate' CHECK (status IN
        ('candidate', 'active', 'superseded')),
    confirmed_by uuid NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, blueprint_version),
    FOREIGN KEY (organization_id, confirmed_by) REFERENCES users(organization_id, id)
);
ALTER TABLE onboarding_cases ADD CONSTRAINT onboarding_blueprint_scope
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id);

CREATE TABLE builds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    build_revision integer NOT NULL CHECK (build_revision > 0),
    state text NOT NULL DEFAULT 'building' CHECK (state IN
        ('building', 'testing', 'admin_preview', 'defect_found', 'fixing',
         'accepted', 'ready_for_release', 'released', 'rolled_back')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, blueprint_id, build_revision),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id)
);

CREATE TABLE defects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    build_id uuid NOT NULL,
    expected text NOT NULL CHECK (length(trim(expected)) > 0),
    observed text NOT NULL CHECK (length(trim(observed)) > 0),
    owner_user_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'fixed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    fixed_at timestamptz,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, build_id) REFERENCES builds(organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id)
);

CREATE TABLE releases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    build_id uuid NOT NULL,
    rollback_release_id uuid,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'rolled_back')),
    approved_by uuid NOT NULL,
    released_at timestamptz NOT NULL DEFAULT now(),
    rolled_back_at timestamptz,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, build_id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id),
    FOREIGN KEY (organization_id, build_id) REFERENCES builds(organization_id, id),
    FOREIGN KEY (organization_id, rollback_release_id) REFERENCES releases(organization_id, id),
    FOREIGN KEY (organization_id, approved_by) REFERENCES users(organization_id, id)
);
CREATE UNIQUE INDEX releases_one_active ON releases(organization_id) WHERE state = 'active';

CREATE TABLE runtime_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid,
    event_type text NOT NULL CHECK (length(trim(event_type)) > 0),
    idempotency_key text NOT NULL CHECK (length(trim(idempotency_key)) > 0),
    correlation_id uuid NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, event_type, idempotency_key),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id)
);
CREATE INDEX runtime_events_tenant_time ON runtime_events(organization_id, occurred_at DESC);

CREATE TABLE outbox_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid,
    event_id uuid NOT NULL,
    topic text NOT NULL CHECK (length(trim(topic)) > 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN
        ('pending', 'leased', 'delivered', 'repair')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    leased_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, event_id, topic),
    FOREIGN KEY (organization_id, event_id) REFERENCES runtime_events(organization_id, id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id)
);
CREATE INDEX outbox_ready ON outbox_jobs(organization_id, available_at)
    WHERE state IN ('pending', 'repair');

CREATE TABLE lifecycle_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    actor_user_id uuid NOT NULL,
    resource_type text NOT NULL CHECK (resource_type IN ('onboarding', 'build', 'release')),
    resource_id uuid NOT NULL,
    from_state text,
    to_state text NOT NULL,
    correlation_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, resource_type, resource_id, correlation_id),
    FOREIGN KEY (organization_id, actor_user_id) REFERENCES users(organization_id, id)
);

CREATE TABLE legacy_inventory (
    organization_id uuid NOT NULL REFERENCES organizations(id),
    source_path text NOT NULL,
    kind text NOT NULL,
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, source_path)
);

CREATE FUNCTION reject_proactive_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'History is append only';
END;
$$;
CREATE TRIGGER runtime_events_immutable BEFORE UPDATE OR DELETE ON runtime_events
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER lifecycle_events_immutable BEFORE UPDATE OR DELETE ON lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER legacy_inventory_immutable BEFORE UPDATE OR DELETE ON legacy_inventory
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();

CREATE FUNCTION preserve_blueprint_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.organization_id <> OLD.organization_id OR NEW.blueprint_version <> OLD.blueprint_version
       OR NEW.content_hash <> OLD.content_hash OR NEW.confirmed_by <> OLD.confirmed_by
       OR NEW.confirmed_at <> OLD.confirmed_at THEN
        RAISE EXCEPTION 'Blueprint identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER blueprints_identity_immutable BEFORE UPDATE ON blueprints
    FOR EACH ROW EXECUTE FUNCTION preserve_blueprint_identity();

CREATE FUNCTION preserve_build_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.organization_id <> OLD.organization_id OR NEW.blueprint_id <> OLD.blueprint_id
       OR NEW.build_revision <> OLD.build_revision OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'Build identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER builds_identity_immutable BEFORE UPDATE ON builds
    FOR EACH ROW EXECUTE FUNCTION preserve_build_identity();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'onboarding_cases', 'blueprints', 'builds', 'defects', 'releases',
        'runtime_events', 'outbox_jobs', 'lifecycle_events', 'legacy_inventory'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO austra_tenant_app', table_name);
        IF table_name IN ('runtime_events', 'outbox_jobs') THEN
            EXECUTE format(
                'CREATE POLICY tenant_read ON %I FOR SELECT TO austra_tenant_app USING '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND (current_setting(''austra.all_departments'', true) = ''on'' '
                || 'OR department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))',
                table_name
            );
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_read ON %I FOR SELECT TO austra_tenant_app USING '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid)',
                table_name
            );
        END IF;
        IF table_name IN ('runtime_events', 'outbox_jobs') THEN
            EXECUTE format(
                'CREATE POLICY tenant_insert ON %I FOR INSERT TO austra_tenant_app WITH CHECK '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' '
                || 'AND (current_setting(''austra.all_departments'', true) = ''on'' '
                || 'OR department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))', table_name);
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_insert ON %I FOR INSERT TO austra_tenant_app WITH CHECK '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'')', table_name);
        END IF;
        IF table_name IN ('runtime_events', 'outbox_jobs') THEN
            EXECUTE format(
                'CREATE POLICY tenant_update ON %I FOR UPDATE TO austra_tenant_app USING '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' '
                || 'AND (current_setting(''austra.all_departments'', true) = ''on'' '
                || 'OR department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '','')))) '
                || 'WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.can_write'', true) = ''on'' '
                || 'AND (current_setting(''austra.all_departments'', true) = ''on'' '
                || 'OR department_id::text = ANY(string_to_array(current_setting(''austra.department_ids'', true), '',''))))', table_name);
        ELSE
            EXECUTE format(
                'CREATE POLICY tenant_update ON %I FOR UPDATE TO austra_tenant_app USING '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'') WITH CHECK '
                || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
                || 'AND current_setting(''austra.all_departments'', true) = ''on'')', table_name);
        END IF;
        EXECUTE format(
            'CREATE POLICY tenant_delete ON %I FOR DELETE TO austra_tenant_app USING '
            || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.all_departments'', true) = ''on'')', table_name);
    END LOOP;
END $$;
