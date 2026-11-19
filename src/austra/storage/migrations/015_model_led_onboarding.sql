ALTER TABLE organization_tours ADD COLUMN desired_outcome text;
ALTER TABLE organization_tours ADD COLUMN summary text;
ALTER TABLE organization_tours ADD COLUMN review_ready boolean NOT NULL DEFAULT false;
ALTER TABLE organization_tours ADD COLUMN website_status text;
ALTER TABLE organization_tours ADD COLUMN revision integer NOT NULL DEFAULT 0;
GRANT DELETE ON organization_tours TO austra_tenant_app;
CREATE POLICY tour_delete ON organization_tours FOR DELETE TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');

CREATE TABLE onboarding_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    position integer NOT NULL,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    body text NOT NULL CHECK (length(trim(body)) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, position)
);
CREATE INDEX onboarding_messages_organization ON onboarding_messages(organization_id, position);
ALTER TABLE onboarding_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_messages FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, DELETE ON onboarding_messages TO austra_tenant_app;
CREATE POLICY messages_read ON onboarding_messages FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid);
CREATE POLICY messages_insert ON onboarding_messages FOR INSERT TO austra_tenant_app
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');
CREATE POLICY messages_delete ON onboarding_messages FOR DELETE TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on');
