-- Restore the candidate sequence on databases created before migration 007 included it.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'blueprint_candidates' AND column_name = 'candidate_order'
    ) THEN
        ALTER TABLE blueprint_candidates ADD COLUMN candidate_order bigint;
        CREATE SEQUENCE blueprint_candidates_order_seq OWNED BY blueprint_candidates.candidate_order;
        ALTER TABLE blueprint_candidates DISABLE TRIGGER blueprint_candidates_immutable;
        WITH ordered AS (
            SELECT id, row_number() OVER (ORDER BY recorded_at, id) AS position
            FROM blueprint_candidates
        )
        UPDATE blueprint_candidates AS candidate SET candidate_order = ordered.position
        FROM ordered WHERE candidate.id = ordered.id;
        ALTER TABLE blueprint_candidates ENABLE TRIGGER blueprint_candidates_immutable;
        ALTER TABLE blueprint_candidates ALTER COLUMN candidate_order
            SET DEFAULT nextval('blueprint_candidates_order_seq');
        ALTER TABLE blueprint_candidates ALTER COLUMN candidate_order SET NOT NULL;
        PERFORM setval(
            'blueprint_candidates_order_seq',
            (SELECT coalesce(max(candidate_order), 0) + 1 FROM blueprint_candidates), false
        );
        CREATE UNIQUE INDEX blueprint_candidates_order_unique ON blueprint_candidates(candidate_order);
        GRANT USAGE, SELECT ON SEQUENCE blueprint_candidates_order_seq TO austra_tenant_app;
    END IF;
END $$;

CREATE TABLE organization_profile_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    organization_name text NOT NULL CHECK (length(trim(organization_name)) > 0),
    about_organization text NOT NULL CHECK (length(trim(about_organization)) > 0),
    desired_outcome text,
    website_url text,
    social_url text,
    confirmed_by uuid,
    confirmed_at timestamptz NOT NULL,
    tour_revision integer NOT NULL CHECK (tour_revision >= 0),
    source text NOT NULL CHECK (source IN ('welcome_confirmation', 'organization_edit', 'legacy_backfill')),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, confirmed_by) REFERENCES users(organization_id, id)
);
CREATE INDEX organization_profile_latest ON organization_profile_revisions
    (organization_id, profile_order DESC);
CREATE TRIGGER organization_profile_revisions_immutable
    BEFORE UPDATE OR DELETE ON organization_profile_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();

INSERT INTO organization_profile_revisions (
    organization_id, organization_name, about_organization, desired_outcome,
    website_url, social_url, confirmed_at, tour_revision, source
)
SELECT tour.organization_id, organization.name, tour.about_organization,
    tour.desired_outcome, tour.website_url, tour.social_url, tour.completed_at,
    tour.revision, 'legacy_backfill'
FROM organization_tours AS tour
JOIN organizations AS organization ON organization.id = tour.organization_id
WHERE tour.completed_at IS NOT NULL AND nullif(trim(tour.about_organization), '') IS NOT NULL;

-- Only repair cases whose outcome exactly matches the discovery summary written by the old route.
UPDATE onboarding_cases AS onboarding SET desired_outcome = tour.desired_outcome
FROM organization_tours AS tour
WHERE onboarding.organization_id = tour.organization_id
  AND tour.desired_outcome IS NOT NULL
  AND onboarding.desired_outcome = tour.discovery_state ->> 'working_summary'
  AND onboarding.state IN ('created', 'collecting', 'clarifying');

ALTER TABLE organization_profile_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_profile_revisions FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON organization_profile_revisions TO austra_tenant_app;
GRANT USAGE, SELECT ON SEQUENCE organization_profile_revisions_profile_order_seq TO austra_tenant_app;
CREATE POLICY profile_read ON organization_profile_revisions FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid);
CREATE POLICY profile_insert ON organization_profile_revisions FOR INSERT TO austra_tenant_app
    WITH CHECK (
        organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND confirmed_by = nullif(current_setting('austra.actor_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on'
    );
