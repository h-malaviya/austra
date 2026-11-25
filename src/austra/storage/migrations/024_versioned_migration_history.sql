-- Existing rows receive a one-time repository baseline in the migration runner.
ALTER TABLE schema_migrations ADD COLUMN version integer;
ALTER TABLE schema_migrations ADD COLUMN checksum text;
ALTER TABLE schema_migrations ADD COLUMN baseline boolean NOT NULL DEFAULT false;
ALTER TABLE schema_migrations ADD CONSTRAINT schema_migrations_version_positive
    CHECK (version IS NULL OR version > 0);
ALTER TABLE schema_migrations ADD CONSTRAINT schema_migrations_checksum_shape
    CHECK (checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX schema_migrations_version_unique ON schema_migrations(version)
    WHERE version IS NOT NULL;
