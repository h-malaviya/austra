-- Pre-checksum installations can record 013 without its patch-build link.
ALTER TABLE defects ADD COLUMN IF NOT EXISTS patch_build_id uuid;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'defects'::regclass AND conname = 'defects_patch_build_scope'
    ) THEN
        ALTER TABLE defects ADD CONSTRAINT defects_patch_build_scope
            FOREIGN KEY (organization_id, patch_build_id) REFERENCES builds (organization_id, id);
    END IF;
END $$;
