ALTER TABLE workflow_automatic_replies
    ADD COLUMN send_attempts integer NOT NULL DEFAULT 0 CHECK (send_attempts >= 0),
    ADD COLUMN receipt_checks integer NOT NULL DEFAULT 0 CHECK (receipt_checks >= 0),
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX workflow_automatic_reply_retry_due ON workflow_automatic_replies (next_attempt_at)
    WHERE state IN ('pending', 'uncertain');
