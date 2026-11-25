-- Let organization admins review rejected source metadata and extracted content.
-- Retrieval still requires included sources and confirmed claims.
CREATE POLICY tenant_admin_review ON evidence_sources FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.all_departments', true) = 'on');

CREATE POLICY tenant_admin_review ON evidence_revisions FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.all_departments', true) = 'on');

CREATE POLICY tenant_admin_review ON evidence_chunks FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.all_departments', true) = 'on');
