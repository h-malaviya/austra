-- Older databases applied semantic blueprints before claim decision ordering existed.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'claim_decisions' AND column_name = 'decision_order'
    ) THEN
        ALTER TABLE claim_decisions ADD COLUMN decision_order bigint;
        CREATE SEQUENCE claim_decisions_order_seq OWNED BY claim_decisions.decision_order;
        ALTER TABLE claim_decisions DISABLE TRIGGER claim_decisions_immutable;
        WITH ordered AS (
            SELECT id, row_number() OVER (ORDER BY recorded_at, id) AS position
            FROM claim_decisions
        )
        UPDATE claim_decisions AS decision SET decision_order = ordered.position
        FROM ordered WHERE decision.id = ordered.id;
        ALTER TABLE claim_decisions ENABLE TRIGGER claim_decisions_immutable;
        ALTER TABLE claim_decisions ALTER COLUMN decision_order
            SET DEFAULT nextval('claim_decisions_order_seq');
        ALTER TABLE claim_decisions ALTER COLUMN decision_order SET NOT NULL;
        PERFORM setval(
            'claim_decisions_order_seq',
            (SELECT coalesce(max(decision_order), 0) + 1 FROM claim_decisions), false
        );
        CREATE UNIQUE INDEX claim_decisions_order_unique ON claim_decisions(decision_order);
        GRANT USAGE, SELECT ON SEQUENCE claim_decisions_order_seq TO austra_tenant_app;
    END IF;
END $$;
