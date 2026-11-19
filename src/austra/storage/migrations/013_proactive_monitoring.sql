ALTER TABLE onboarding_cases DROP CONSTRAINT onboarding_cases_organization_id_key;
CREATE UNIQUE INDEX onboarding_one_unlocked ON onboarding_cases (organization_id)
    WHERE state <> 'locked_candidate';
ALTER TABLE defects ADD COLUMN patch_build_id uuid;
ALTER TABLE defects ADD CONSTRAINT defects_patch_build_scope
    FOREIGN KEY (organization_id, patch_build_id) REFERENCES builds(organization_id, id);

CREATE TABLE review_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    trigger_kind text NOT NULL CHECK (trigger_kind IN
        ('changed_evidence', 'failed_run', 'policy_expiry', 'schema_drift',
         'outcome_conflict', 'business_meaning_change')),
    subject_id uuid NOT NULL,
    blueprint_id uuid,
    owner_user_id uuid NOT NULL,
    citations jsonb NOT NULL CHECK (jsonb_typeof(citations) = 'array'
        AND jsonb_array_length(citations) > 0),
    impact text NOT NULL CHECK (length(trim(impact)) > 0),
    next_step text NOT NULL CHECK (length(trim(next_step)) > 0),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved')),
    opened_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id)
);
CREATE UNIQUE INDEX review_cases_open_subject ON review_cases
    (organization_id, trigger_kind, subject_id) WHERE state = 'open';
CREATE INDEX review_cases_owner ON review_cases
    (organization_id, owner_user_id, opened_at DESC) WHERE state = 'open';

CREATE TABLE operational_metrics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    task_id uuid,
    metric text NOT NULL CHECK (metric IN
        ('retrieval_miss', 'citation_failure', 'token_budget_breach',
         'task_resolved', 'task_cost_usd', 'task_latency_ms',
         'connection_failure', 'release_defect', 'repair_time_ms')),
    value numeric(20,10) NOT NULL CHECK (value >= 0),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id)
);
CREATE INDEX operational_metrics_time ON operational_metrics
    (organization_id, metric, recorded_at DESC);
CREATE TRIGGER operational_metrics_immutable BEFORE UPDATE OR DELETE ON operational_metrics
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();

DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['review_cases', 'operational_metrics'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', name);
        EXECUTE format('GRANT SELECT, INSERT ON %I TO austra_tenant_app', name);
        EXECUTE format('CREATE POLICY scoped_read ON %I FOR SELECT TO austra_tenant_app USING '
            || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.all_departments'', true) = ''on'')', name);
        EXECUTE format('CREATE POLICY scoped_insert ON %I FOR INSERT TO austra_tenant_app WITH CHECK '
            || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.can_write'', true) = ''on'')', name);
    END LOOP;
END $$;
GRANT UPDATE ON review_cases TO austra_tenant_app;
DROP POLICY scoped_insert ON operational_metrics;
CREATE POLICY scoped_insert ON operational_metrics FOR INSERT TO austra_tenant_app WITH CHECK
    (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
     AND nullif(current_setting('austra.actor_id', true), '') IS NOT NULL);
DROP POLICY scoped_read ON review_cases;
CREATE POLICY scoped_read ON review_cases FOR SELECT TO austra_tenant_app USING
    (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid);
CREATE POLICY scoped_update ON review_cases FOR UPDATE TO austra_tenant_app USING
    (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
     AND current_setting('austra.can_write', true) = 'on') WITH CHECK
    (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
     AND current_setting('austra.can_write', true) = 'on');
