ALTER TABLE capability_records DROP CONSTRAINT capability_records_kind_check;
ALTER TABLE capability_records ADD CONSTRAINT capability_records_kind_check
    CHECK (kind ~ '^[a-z][a-z0-9_]{0,79}$');
CREATE INDEX capability_records_projection_idx
    ON capability_records (organization_id, department_id, kind, state, created_at, id);
