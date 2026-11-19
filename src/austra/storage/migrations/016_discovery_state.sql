ALTER TABLE organization_tours
    ADD COLUMN discovery_state jsonb NOT NULL DEFAULT '{}'::jsonb;
