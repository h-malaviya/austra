-- Only explicitly tracked delivered Gmail jobs are eligible for automatic thread checks.
ALTER TABLE outbox_jobs
    ADD COLUMN reply_check_at timestamptz,
    ADD COLUMN reply_tracking_until timestamptz,
    ADD COLUMN reply_check_failed boolean NOT NULL DEFAULT false;

CREATE INDEX outbox_gmail_reply_due ON outbox_jobs (reply_check_at, id)
    WHERE state = 'delivered' AND topic = 'capability.email' AND reply_check_at IS NOT NULL;
