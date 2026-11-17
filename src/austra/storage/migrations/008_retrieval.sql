ALTER TABLE evidence_sources ADD COLUMN department_id uuid;
ALTER TABLE blueprints ADD COLUMN effective_at timestamptz;
ALTER TABLE evidence_sources ADD COLUMN owner_user_id uuid;
ALTER TABLE evidence_sources ADD COLUMN access_label text NOT NULL DEFAULT 'organization'
    CHECK (access_label IN ('organization', 'department', 'owner', 'restricted'));
ALTER TABLE evidence_sources ADD CONSTRAINT evidence_source_department
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id);
ALTER TABLE evidence_sources ADD CONSTRAINT evidence_source_owner
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id);

CREATE INDEX evidence_source_access ON evidence_sources
    (organization_id, department_id, access_label, id);
CREATE INDEX evidence_revision_visible ON evidence_revisions
    (organization_id, source_id, captured_at DESC) WHERE status = 'complete';
CREATE INDEX semantic_lookup ON semantic_objects (organization_id, kind, stable_key);
CREATE INDEX blueprint_active_lookup ON blueprints
    (organization_id, confirmed_at DESC) WHERE status = 'active';
CREATE INDEX business_object_exact ON business_objects
    (organization_id, kind, external_id) WHERE archived_at IS NULL;
ALTER TABLE business_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE business_objects FORCE ROW LEVEL SECURITY;
GRANT SELECT ON business_objects TO austra_tenant_app;
CREATE POLICY retrieval_read ON business_objects FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR department_id::text = ANY(string_to_array(
                current_setting('austra.department_ids', true), ','))));

CREATE TABLE semantic_revision_embeddings (
    organization_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    embedding_model text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    embedding public.vector(384) NOT NULL,
    PRIMARY KEY (organization_id, revision_id, embedding_model),
    FOREIGN KEY (organization_id, revision_id)
        REFERENCES semantic_revisions(organization_id, id)
);
CREATE INDEX semantic_embedding_scope ON semantic_revision_embeddings
    (organization_id, embedding_model, revision_id);
ALTER TABLE semantic_revision_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_revision_embeddings FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON semantic_revision_embeddings TO austra_tenant_app;
CREATE POLICY tenant_read ON semantic_revision_embeddings FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid);
CREATE POLICY tenant_insert ON semantic_revision_embeddings FOR INSERT TO austra_tenant_app
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');

CREATE FUNCTION retrieval_source_visible(source evidence_sources) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $body$
    SELECT source.organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND source.excluded = false
        AND (current_setting('austra.all_departments', true) = 'on'
            OR source.access_label = 'organization'
            OR (source.access_label = 'department' AND source.department_id::text = ANY(
                string_to_array(current_setting('austra.department_ids', true), ',')))
            OR (source.access_label = 'owner' AND source.owner_user_id::text =
                current_setting('austra.actor_id', true)));
$body$;
REVOKE ALL ON FUNCTION retrieval_source_visible(evidence_sources) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retrieval_source_visible(evidence_sources) TO austra_tenant_app;

DROP POLICY tenant_read ON evidence_sources;
CREATE POLICY tenant_read ON evidence_sources FOR SELECT TO austra_tenant_app
    USING (retrieval_source_visible(evidence_sources));
DROP POLICY tenant_read ON evidence_revisions;
CREATE POLICY tenant_read ON evidence_revisions FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND EXISTS (SELECT 1 FROM evidence_sources s WHERE s.id = source_id
            AND retrieval_source_visible(s)));
DROP POLICY tenant_read ON evidence_chunks;
CREATE POLICY tenant_read ON evidence_chunks FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND EXISTS (SELECT 1 FROM evidence_revisions r JOIN evidence_sources s ON s.id = r.source_id
            WHERE r.id = revision_id AND retrieval_source_visible(s)));

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['blueprints', 'blueprint_members', 'semantic_objects',
        'semantic_revisions', 'semantic_edges', 'semantic_revision_claims', 'source_claims',
        'claim_decisions', 'claim_conflicts'] LOOP
        EXECUTE format('DROP POLICY tenant_read ON %I', table_name);
        EXECUTE format('CREATE POLICY tenant_read ON %I FOR SELECT TO austra_tenant_app '
            || 'USING (organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid)',
            table_name);
    END LOOP;
END $$;
