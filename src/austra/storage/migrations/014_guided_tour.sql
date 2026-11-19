CREATE TABLE organization_tours (
    organization_id uuid PRIMARY KEY REFERENCES organizations(id),
    about_admin text,
    about_organization text,
    website_url text,
    social_url text,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE organization_tours ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_tours FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON organization_tours TO austra_tenant_app;
CREATE POLICY tour_read ON organization_tours FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid);
CREATE POLICY tour_insert ON organization_tours FOR INSERT TO austra_tenant_app
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');
CREATE POLICY tour_update ON organization_tours FOR UPDATE TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on')
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');
