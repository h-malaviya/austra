ALTER TABLE evidence_sources ADD COLUMN review_state text NOT NULL DEFAULT 'included'
    CHECK (review_state IN ('pending', 'included', 'excluded'));
