ALTER TABLE onboarding_cases ADD COLUMN workflow_name text,
    ADD COLUMN parent_case_id uuid,
    ADD COLUMN workflow_root_id uuid,
    ADD COLUMN discovery_context jsonb NOT NULL DEFAULT '{}',
    ADD FOREIGN KEY (organization_id, workflow_root_id) REFERENCES onboarding_cases (organization_id, id),
    ADD FOREIGN KEY (organization_id, parent_case_id)
        REFERENCES onboarding_cases (organization_id, id);
ALTER TABLE onboarding_messages ADD COLUMN workflow_scope uuid;
CREATE INDEX onboarding_workflow_history ON onboarding_messages (organization_id, workflow_scope, position);
DROP INDEX onboarding_one_unlocked;
CREATE UNIQUE INDEX onboarding_one_open_intake ON onboarding_cases (organization_id)
    WHERE state <> 'locked_candidate' AND parent_case_id IS NULL;
CREATE INDEX onboarding_open_workflow_name
    ON onboarding_cases (organization_id, lower(workflow_name))
    WHERE workflow_name IS NOT NULL AND state <> 'locked_candidate';

-- Keep the current review and any confirmed blueprint on the original case.
-- Older, independently named chat proposals retain their IDs, hashes and history.
UPDATE onboarding_cases c SET workflow_name = (
    SELECT nullif(b.proposal->>'name', '') FROM blueprint_candidates b
    WHERE b.organization_id = c.organization_id AND b.case_id = c.id
    ORDER BY candidate_order DESC LIMIT 1
);
ALTER TABLE blueprint_candidates DISABLE TRIGGER blueprint_candidates_immutable;
DO $$
DECLARE item record; new_case uuid;
BEGIN
    FOR item IN
        SELECT DISTINCT ON (c.id, lower(b.proposal->>'name'))
            c.organization_id, c.id AS parent, b.proposal->>'name' AS name, b.id AS origin
        FROM onboarding_cases c JOIN blueprint_candidates b
          ON (b.organization_id, b.case_id) = (c.organization_id, c.id)
        WHERE lower(b.proposal->>'name') <> lower(c.workflow_name)
          AND b.proposal->'revision_ids' = '[]'::jsonb
        ORDER BY c.id, lower(b.proposal->>'name'), candidate_order DESC
    LOOP
        INSERT INTO onboarding_cases (organization_id, parent_case_id, workflow_name, created_event_id, state)
        VALUES (item.organization_id, item.parent, item.name, item.origin, 'clarifying')
        RETURNING id INTO new_case;
        UPDATE blueprint_candidates SET case_id = new_case
        WHERE organization_id = item.organization_id AND case_id = item.parent
          AND lower(proposal->>'name') = lower(item.name) AND proposal->'revision_ids' = '[]'::jsonb;
    END LOOP;
END $$;
ALTER TABLE blueprint_candidates ENABLE TRIGGER blueprint_candidates_immutable;
-- Resume old conversations from the draft. Earlier messages may describe several workflows.
UPDATE organization_tours t SET discovery_state = jsonb_build_object(
    'workflow_case_id', b.case_id, 'workflow_candidate_id', b.id, 'memory_id', b.case_id,
    'unresolved_questions', coalesce(b.proposal->'open_decisions', '[]'::jsonb))
FROM blueprint_candidates b WHERE b.organization_id = t.organization_id
    AND b.id::text = t.discovery_state->>'workflow_candidate_id';

ALTER TABLE releases ADD COLUMN workflow_case_id uuid,
    ADD FOREIGN KEY (organization_id, workflow_case_id) REFERENCES onboarding_cases (organization_id, id);
UPDATE releases r SET workflow_case_id = c.id FROM onboarding_cases c
WHERE (r.organization_id, r.blueprint_id) = (c.organization_id, c.blueprint_id)
    AND c.workflow_name IS NOT NULL AND r.rollback_release_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM releases next WHERE next.rollback_release_id = r.id);
DROP INDEX releases_one_active;
CREATE UNIQUE INDEX releases_one_active_workflow ON releases (organization_id, workflow_case_id)
    NULLS NOT DISTINCT WHERE state = 'active';
