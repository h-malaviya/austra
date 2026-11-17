CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

ALTER TABLE onboarding_cases ADD COLUMN business_description text;
ALTER TABLE onboarding_cases ADD COLUMN desired_outcome text;

CREATE TABLE evidence_sources (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    case_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('file', 'website', 'linkedin_export', 'linkedin_manual')),
    source_key text NOT NULL,
    display_name text NOT NULL,
    excluded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, kind, source_key),
    FOREIGN KEY (organization_id, case_id) REFERENCES onboarding_cases(organization_id, id)
);

CREATE TABLE evidence_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    source_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    blob_key text NOT NULL,
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    media_type text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'complete', 'partial', 'failed')),
    total_units integer,
    extracted_units integer NOT NULL DEFAULT 0,
    error text,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, source_id, revision),
    FOREIGN KEY (organization_id, source_id) REFERENCES evidence_sources(organization_id, id)
);
CREATE INDEX evidence_revisions_source ON evidence_revisions(organization_id, source_id, revision DESC);

CREATE TABLE evidence_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    locator text NOT NULL,
    text text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    token_count integer NOT NULL CHECK (token_count > 0),
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding public.vector(384),
    embedding_model text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, revision_id, locator, content_hash),
    FOREIGN KEY (organization_id, revision_id) REFERENCES evidence_revisions(organization_id, id)
);
CREATE INDEX evidence_chunks_search ON evidence_chunks USING gin(search_vector);
CREATE INDEX evidence_chunks_revision ON evidence_chunks(organization_id, revision_id);

CREATE TABLE evidence_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    cursor integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'complete', 'partial', 'failed')),
    leased_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, revision_id),
    FOREIGN KEY (organization_id, revision_id) REFERENCES evidence_revisions(organization_id, id)
);

CREATE TRIGGER evidence_chunks_immutable BEFORE UPDATE OR DELETE ON evidence_chunks
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER evidence_revisions_immutable BEFORE DELETE ON evidence_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['evidence_sources', 'evidence_revisions', 'evidence_chunks', 'evidence_jobs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO austra_tenant_app', table_name);
        EXECUTE format('CREATE POLICY tenant_read ON %I FOR SELECT TO austra_tenant_app USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid AND current_setting(''austra.all_departments'', true) = ''on'')', table_name);
        EXECUTE format('CREATE POLICY tenant_insert ON %I FOR INSERT TO austra_tenant_app WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid AND current_setting(''austra.all_departments'', true) = ''on'' AND current_setting(''austra.can_write'', true) = ''on'')', table_name);
        EXECUTE format('CREATE POLICY tenant_update ON %I FOR UPDATE TO austra_tenant_app USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid AND current_setting(''austra.all_departments'', true) = ''on'' AND current_setting(''austra.can_write'', true) = ''on'') WITH CHECK (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid AND current_setting(''austra.all_departments'', true) = ''on'' AND current_setting(''austra.can_write'', true) = ''on'')', table_name);
        EXECUTE format('CREATE POLICY tenant_delete ON %I FOR DELETE TO austra_tenant_app USING (false)', table_name);
    END LOOP;
END $$;
