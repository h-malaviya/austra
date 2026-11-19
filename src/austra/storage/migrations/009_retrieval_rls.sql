DROP POLICY tenant_read ON source_claims;
CREATE POLICY tenant_read ON source_claims FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR EXISTS (SELECT 1 FROM evidence_chunks c
                WHERE c.id = source_claims.chunk_id)));

DROP POLICY tenant_read ON semantic_revisions;
CREATE POLICY tenant_read ON semantic_revisions FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR (EXISTS (SELECT 1 FROM semantic_revision_claims link
                WHERE link.revision_id = semantic_revisions.id)
                AND (SELECT count(*) FROM semantic_revision_claims link
                     WHERE link.revision_id = semantic_revisions.id)
                  = (SELECT count(*) FROM semantic_revision_claims link
                     JOIN source_claims claim ON claim.id = link.claim_id
                     JOIN evidence_chunks chunk ON chunk.id = claim.chunk_id
                     WHERE link.revision_id = semantic_revisions.id))));

DROP POLICY tenant_read ON semantic_objects;
CREATE POLICY tenant_read ON semantic_objects FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR EXISTS (SELECT 1 FROM semantic_revisions revision
                WHERE revision.object_id = semantic_objects.id)));

DROP POLICY tenant_read ON semantic_revision_embeddings;
CREATE POLICY tenant_read ON semantic_revision_embeddings FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR EXISTS (SELECT 1 FROM semantic_revisions revision
                WHERE revision.id = semantic_revision_embeddings.revision_id)));

DROP POLICY tenant_read ON blueprint_members;
CREATE POLICY tenant_read ON blueprint_members FOR SELECT TO austra_tenant_app
    USING (organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
        AND (current_setting('austra.all_departments', true) = 'on'
            OR EXISTS (SELECT 1 FROM semantic_revisions revision
                WHERE revision.id = blueprint_members.revision_id)));
