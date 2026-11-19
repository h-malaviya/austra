CREATE FUNCTION preserve_evidence_revision_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.organization_id <> OLD.organization_id OR NEW.source_id <> OLD.source_id
       OR NEW.revision <> OLD.revision OR NEW.sha256 <> OLD.sha256
       OR NEW.blob_key <> OLD.blob_key OR NEW.byte_count <> OLD.byte_count
       OR NEW.media_type <> OLD.media_type OR NEW.captured_at <> OLD.captured_at THEN
        RAISE EXCEPTION 'Evidence revision identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER evidence_revisions_identity_immutable BEFORE UPDATE ON evidence_revisions
    FOR EACH ROW EXECUTE FUNCTION preserve_evidence_revision_identity();
