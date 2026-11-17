CREATE TABLE semantic_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    kind text NOT NULL CHECK (kind IN ('capability','actor','authority','policy','knowledge',
        'event','workflow','business_object','integration','projection')),
    stable_key text NOT NULL CHECK (stable_key ~ '^[a-z][a-z0-9_]*$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, kind, stable_key)
);
CREATE TABLE source_claims (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    proposition text NOT NULL CHECK (length(trim(proposition)) > 0),
    confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, chunk_id) REFERENCES evidence_chunks(organization_id, id)
);
CREATE TABLE claim_conflicts (
    organization_id uuid NOT NULL,
    first_claim_id uuid NOT NULL,
    second_claim_id uuid NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY (organization_id, first_claim_id, second_claim_id),
    CHECK (first_claim_id < second_claim_id),
    FOREIGN KEY (organization_id, first_claim_id) REFERENCES source_claims(organization_id, id),
    FOREIGN KEY (organization_id, second_claim_id) REFERENCES source_claims(organization_id, id)
);
CREATE TABLE claim_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    organization_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('confirmed','rejected','superseded')),
    decided_by uuid NOT NULL,
    rationale text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, claim_id) REFERENCES source_claims(organization_id, id),
    FOREIGN KEY (organization_id, decided_by) REFERENCES users(organization_id, id)
);
CREATE INDEX claim_decisions_latest ON claim_decisions(organization_id, claim_id, recorded_at DESC);
CREATE TABLE semantic_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    object_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    owner_user_id uuid,
    rationale text NOT NULL CHECK (length(trim(rationale)) > 0),
    content jsonb NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    supersedes_id uuid,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, object_id, revision),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    FOREIGN KEY (organization_id, object_id) REFERENCES semantic_objects(organization_id, id),
    FOREIGN KEY (organization_id, owner_user_id) REFERENCES users(organization_id, id),
    FOREIGN KEY (organization_id, supersedes_id) REFERENCES semantic_revisions(organization_id, id)
);
CREATE INDEX semantic_revision_time ON semantic_revisions(organization_id, object_id, recorded_at DESC);
CREATE TABLE semantic_revision_claims (
    organization_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    claim_id uuid NOT NULL,
    PRIMARY KEY (organization_id, revision_id, claim_id),
    FOREIGN KEY (organization_id, revision_id) REFERENCES semantic_revisions(organization_id, id),
    FOREIGN KEY (organization_id, claim_id) REFERENCES source_claims(organization_id, id)
);
CREATE TABLE semantic_edges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    from_revision_id uuid NOT NULL,
    relation text NOT NULL CHECK (relation IN ('authorizes','governs','supports','triggers',
        'uses','reads','writes','projects','owned_by','invokes','constrains')),
    to_revision_id uuid NOT NULL,
    rationale text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, from_revision_id, relation, to_revision_id),
    CHECK (from_revision_id <> to_revision_id),
    FOREIGN KEY (organization_id, from_revision_id) REFERENCES semantic_revisions(organization_id, id),
    FOREIGN KEY (organization_id, to_revision_id) REFERENCES semantic_revisions(organization_id, id)
);
CREATE INDEX semantic_edges_reverse ON semantic_edges(organization_id, to_revision_id, relation);
CREATE TABLE blueprint_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    organization_id uuid NOT NULL,
    case_id uuid NOT NULL,
    proposal jsonb NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    proposed_by uuid NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    FOREIGN KEY (organization_id, case_id) REFERENCES onboarding_cases(organization_id, id),
    FOREIGN KEY (organization_id, proposed_by) REFERENCES users(organization_id, id)
);
CREATE INDEX blueprint_candidates_latest ON blueprint_candidates(organization_id, case_id, recorded_at DESC);
CREATE TABLE blueprint_members (
    organization_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    PRIMARY KEY (organization_id, blueprint_id, revision_id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id),
    FOREIGN KEY (organization_id, revision_id) REFERENCES semantic_revisions(organization_id, id)
);
CREATE TABLE blueprint_edges (
    organization_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    edge_id uuid NOT NULL,
    PRIMARY KEY (organization_id, blueprint_id, edge_id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id),
    FOREIGN KEY (organization_id, edge_id) REFERENCES semantic_edges(organization_id, id)
);
CREATE TABLE blueprint_manifests (
    organization_id uuid NOT NULL,
    blueprint_id uuid NOT NULL,
    manifest jsonb NOT NULL,
    compiled_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, blueprint_id),
    FOREIGN KEY (organization_id, blueprint_id) REFERENCES blueprints(organization_id, id)
);
DO $$
BEGIN
    EXECUTE format($definition$
        CREATE FUNCTION semantic_owner_active(target_user_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $body$
            SELECT EXISTS (
                SELECT 1 FROM %I.users WHERE id = target_user_id
                AND organization_id = nullif(current_setting('austra.organization_id', true), '')::uuid
                AND status = 'active'
            );
        $body$;
    $definition$, current_schema());
END $$;
REVOKE ALL ON FUNCTION semantic_owner_active(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION semantic_owner_active(uuid) TO austra_tenant_app;
CREATE TRIGGER source_claims_immutable BEFORE UPDATE OR DELETE ON source_claims
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER claim_conflicts_immutable BEFORE UPDATE OR DELETE ON claim_conflicts
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER claim_decisions_immutable BEFORE UPDATE OR DELETE ON claim_decisions
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER semantic_objects_immutable BEFORE UPDATE OR DELETE ON semantic_objects
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER semantic_revisions_immutable BEFORE UPDATE OR DELETE ON semantic_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER semantic_revision_claims_immutable BEFORE UPDATE OR DELETE ON semantic_revision_claims
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER semantic_edges_immutable BEFORE UPDATE OR DELETE ON semantic_edges
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER blueprint_candidates_immutable BEFORE UPDATE OR DELETE ON blueprint_candidates
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER blueprint_members_immutable BEFORE UPDATE OR DELETE ON blueprint_members
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER blueprint_edges_immutable BEFORE UPDATE OR DELETE ON blueprint_edges
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
CREATE TRIGGER blueprint_manifests_immutable BEFORE UPDATE OR DELETE ON blueprint_manifests
    FOR EACH ROW EXECUTE FUNCTION reject_proactive_history_change();
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['semantic_objects','source_claims','claim_conflicts',
        'claim_decisions','semantic_revisions','semantic_revision_claims','semantic_edges',
        'blueprint_candidates','blueprint_members','blueprint_edges','blueprint_manifests'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('GRANT SELECT, INSERT ON %I TO austra_tenant_app', table_name);
        EXECUTE format('CREATE POLICY tenant_read ON %I FOR SELECT TO austra_tenant_app USING '
            || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.all_departments'', true) = ''on'')', table_name);
        EXECUTE format('CREATE POLICY tenant_insert ON %I FOR INSERT TO austra_tenant_app WITH CHECK '
            || '(organization_id = nullif(current_setting(''austra.organization_id'', true), '''')::uuid '
            || 'AND current_setting(''austra.all_departments'', true) = ''on'' '
            || 'AND current_setting(''austra.can_write'', true) = ''on'')', table_name);
    END LOOP;
END $$;
