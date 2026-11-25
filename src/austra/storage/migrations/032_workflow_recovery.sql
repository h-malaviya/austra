ALTER TABLE outbox_jobs
    ADD COLUMN reply_recovery jsonb NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(reply_recovery) = 'object');

-- The previous worker saved evidence and applied it in a single transaction.
ALTER TABLE workflow_reply_evidence ADD COLUMN applied_at timestamptz DEFAULT now();
ALTER TABLE workflow_reply_evidence ALTER COLUMN applied_at DROP DEFAULT;
GRANT UPDATE (applied_at) ON workflow_reply_evidence TO austra_tenant_app;
