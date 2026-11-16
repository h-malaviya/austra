CREATE TABLE business_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    department_id uuid,
    kind text NOT NULL,
    external_id text NOT NULL,
    source_path text NOT NULL,
    archived_at timestamptz,
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments (organization_id, id),
    UNIQUE (organization_id, kind, external_id)
);
CREATE INDEX business_objects_department_kind
    ON business_objects (organization_id, department_id, kind)
    WHERE archived_at IS NULL;

CREATE FUNCTION reject_business_tenant_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.organization_id <> OLD.organization_id THEN
        RAISE EXCEPTION 'Business object organization is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER business_objects_tenant_immutable BEFORE UPDATE ON business_objects
    FOR EACH ROW EXECUTE FUNCTION reject_business_tenant_change();
