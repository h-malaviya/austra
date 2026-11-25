ALTER TABLE outbox_jobs
    ADD COLUMN follow_up_days integer CHECK (follow_up_days IN (1, 2, 7)),
    ADD COLUMN follow_up_due_at timestamptz,
    ADD COLUMN owner_email_alerts boolean NOT NULL DEFAULT false;

CREATE INDEX outbox_supplier_follow_up_due ON outbox_jobs (follow_up_due_at)
    WHERE state = 'delivered' AND topic = 'capability.email' AND follow_up_due_at IS NOT NULL;

CREATE TABLE workflow_reply_evidence (
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    job_id uuid NOT NULL,
    message_id text NOT NULL,
    content jsonb NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, job_id, message_id),
    FOREIGN KEY (organization_id, job_id) REFERENCES outbox_jobs(organization_id, id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id)
);

CREATE TABLE workflow_notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    job_id uuid NOT NULL,
    event_key text NOT NULL,
    title text NOT NULL,
    message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz,
    email_state text NOT NULL DEFAULT 'off'
        CHECK (email_state IN ('off', 'pending', 'dispatching', 'sent', 'attention')),
    email_attempted_at timestamptz,
    UNIQUE (organization_id, job_id, event_key),
    FOREIGN KEY (organization_id, job_id) REFERENCES outbox_jobs(organization_id, id),
    FOREIGN KEY (organization_id, department_id) REFERENCES departments(organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id)
);

CREATE INDEX workflow_notifications_owner ON workflow_notifications
    (organization_id, owner_user_id, created_at DESC);
CREATE INDEX workflow_notifications_email_due ON workflow_notifications (created_at)
    WHERE email_state = 'pending';
CREATE INDEX workflow_notifications_email_attempt ON workflow_notifications (email_attempted_at)
    WHERE email_state = 'dispatching';

ALTER TABLE workflow_reply_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_reply_evidence FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON workflow_reply_evidence TO austra_tenant_app;
CREATE POLICY reply_evidence_department ON workflow_reply_evidence TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
             OR department_id::text = ANY(string_to_array(current_setting('austra.department_ids', true), ','))))
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on'
        AND (current_setting('austra.all_departments', true) = 'on'
             OR department_id::text = ANY(string_to_array(current_setting('austra.department_ids', true), ','))));

ALTER TABLE workflow_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_notifications FORCE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON workflow_notifications TO austra_tenant_app;
CREATE POLICY workflow_notifications_read ON workflow_notifications FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
             OR department_id::text = ANY(string_to_array(current_setting('austra.department_ids', true), ','))));
CREATE POLICY workflow_notifications_mark ON workflow_notifications FOR UPDATE TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND owner_user_id = nullif(current_setting('austra.actor_id', true), '')::uuid);
CREATE POLICY workflow_notifications_enqueue ON workflow_notifications FOR INSERT TO austra_tenant_app
    WITH CHECK (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND current_setting('austra.can_write', true) = 'on'
        AND (current_setting('austra.all_departments', true) = 'on'
             OR department_id::text = ANY(string_to_array(current_setting('austra.department_ids', true), ','))));
