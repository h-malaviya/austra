CREATE TABLE department_workflow_plans (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    messages jsonb NOT NULL DEFAULT '[]',
    unresolved jsonb NOT NULL DEFAULT '[]',
    proposal jsonb,
    submitted_at timestamptz,
    case_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id),
    FOREIGN KEY (organization_id, case_id) REFERENCES onboarding_cases(organization_id, id),
    UNIQUE (organization_id, case_id)
);
CREATE INDEX department_workflow_plans_owner ON department_workflow_plans (organization_id, owner_user_id, updated_at DESC);
ALTER TABLE department_workflow_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE department_workflow_plans FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON department_workflow_plans TO austra_tenant_app;
CREATE POLICY department_workflow_plans_read ON department_workflow_plans FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid AND (
        (owner_user_id::text = current_setting('austra.actor_id', true) AND (
            current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
                string_to_array(current_setting('austra.department_ids', true), ',')))) OR
        (current_setting('austra.all_departments', true) = 'on' AND submitted_at IS NOT NULL)));
CREATE POLICY department_workflow_plans_insert ON department_workflow_plans FOR INSERT TO austra_tenant_app
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id::text = current_setting('austra.actor_id', true) AND case_id IS NULL
        AND (current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
            string_to_array(current_setting('austra.department_ids', true), ','))));
CREATE POLICY department_workflow_plans_update ON department_workflow_plans FOR UPDATE TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid AND (
        (owner_user_id::text = current_setting('austra.actor_id', true) AND submitted_at IS NULL AND (
            current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
                string_to_array(current_setting('austra.department_ids', true), ',')))) OR
        (current_setting('austra.all_departments', true) = 'on' AND submitted_at IS NOT NULL)))
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid AND (
        (owner_user_id::text = current_setting('austra.actor_id', true) AND case_id IS NULL AND (
            current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
                string_to_array(current_setting('austra.department_ids', true), ',')))) OR
        (current_setting('austra.all_departments', true) = 'on' AND submitted_at IS NOT NULL)));

ALTER TABLE outbox_jobs ADD COLUMN automatic_reply_approved_by uuid;
ALTER TABLE outbox_jobs ADD FOREIGN KEY (organization_id, automatic_reply_approved_by) REFERENCES users(organization_id,id);

CREATE TABLE workflow_automatic_replies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    job_id uuid NOT NULL,
    message_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','sending','sent','completed','needs_review','uncertain')),
    outcome jsonb NOT NULL,
    receipt text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, job_id, message_id),
    FOREIGN KEY (organization_id, job_id, message_id) REFERENCES workflow_reply_evidence(organization_id,job_id,message_id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id,id)
);
CREATE INDEX workflow_automatic_reply_ready ON workflow_automatic_replies (created_at)
    WHERE state IN ('pending','sending');
ALTER TABLE workflow_automatic_replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_automatic_replies FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON workflow_automatic_replies TO austra_tenant_app;
CREATE POLICY workflow_automatic_replies_department ON workflow_automatic_replies TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
            string_to_array(current_setting('austra.department_ids', true), ','))))
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on'
        AND (current_setting('austra.all_departments', true) = 'on' OR department_id::text = ANY(
            string_to_array(current_setting('austra.department_ids', true), ','))));
