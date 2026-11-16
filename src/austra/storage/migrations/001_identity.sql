CREATE TABLE organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    name text NOT NULL CHECK (length(trim(name)) > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    suspended_at timestamptz,
    deleted_at timestamptz
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES organizations(id),
    username_normalized text NOT NULL
        CHECK (username_normalized = lower(trim(username_normalized))
               AND length(username_normalized) > 0),
    role text NOT NULL CHECK (role IN ('super_admin', 'org_admin', 'employee')),
    password_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    must_change_password boolean NOT NULL DEFAULT true,
    deleted_at timestamptz,
    CONSTRAINT user_role_scope CHECK (
        (role = 'super_admin' AND organization_id IS NULL)
        OR (role IN ('org_admin', 'employee') AND organization_id IS NOT NULL)
    ),
    UNIQUE (organization_id, id)
);
CREATE UNIQUE INDEX users_organization_username_active
    ON users (organization_id, username_normalized)
    WHERE organization_id IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX users_platform_username_active
    ON users (username_normalized)
    WHERE organization_id IS NULL AND deleted_at IS NULL;

CREATE TABLE sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_active ON sessions (user_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE support_contexts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES sessions(id),
    super_admin_user_id uuid NOT NULL REFERENCES users(id),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    reason text NOT NULL CHECK (length(trim(reason)) > 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ended_at timestamptz,
    UNIQUE (id, organization_id)
);
CREATE UNIQUE INDEX support_contexts_active_session
    ON support_contexts (session_id) WHERE ended_at IS NULL;

CREATE TABLE departments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    name text NOT NULL CHECK (length(trim(name)) > 0),
    archived_at timestamptz,
    archived_by uuid REFERENCES users(id),
    archive_reason text,
    UNIQUE (organization_id, id)
);
CREATE UNIQUE INDEX departments_active_slug
    ON departments (organization_id, slug) WHERE archived_at IS NULL;

CREATE TABLE department_memberships (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    user_id uuid NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments (organization_id, id),
    FOREIGN KEY (organization_id, user_id)
        REFERENCES users (organization_id, id),
    UNIQUE (id, organization_id, department_id)
);
CREATE UNIQUE INDEX department_memberships_active_user
    ON department_memberships (department_id, user_id) WHERE revoked_at IS NULL;

CREATE TABLE department_roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    name text NOT NULL CHECK (length(trim(name)) > 0),
    current_version integer CHECK (current_version > 0),
    archived_at timestamptz,
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments (organization_id, id),
    UNIQUE (id, organization_id, department_id)
);
CREATE UNIQUE INDEX department_roles_active_slug
    ON department_roles (department_id, slug) WHERE archived_at IS NULL;

CREATE TABLE role_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id uuid NOT NULL REFERENCES department_roles(id),
    version integer NOT NULL CHECK (version > 0),
    change_reason text NOT NULL CHECK (length(trim(change_reason)) > 0),
    approved_by uuid NOT NULL REFERENCES users(id),
    effective_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (role_id, version)
);

CREATE TABLE role_permissions (
    role_version_id uuid NOT NULL REFERENCES role_versions(id),
    permission_key text NOT NULL CHECK (permission_key IN (
        'chat.use', 'records.read', 'records.write', 'evidence.read',
        'policies.read', 'policies.draft', 'workflows.read',
        'workflows.draft', 'workflows.run', 'reports.read',
        'integrations.use', 'department.members.manage',
        'department.users.create', 'department.roles.assign'
    )),
    PRIMARY KEY (role_version_id, permission_key)
);

CREATE TABLE role_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    department_membership_id uuid NOT NULL,
    role_id uuid NOT NULL,
    assigned_by uuid NOT NULL REFERENCES users(id),
    revoked_at timestamptz,
    FOREIGN KEY (department_membership_id, organization_id, department_id)
        REFERENCES department_memberships (id, organization_id, department_id),
    FOREIGN KEY (role_id, organization_id, department_id)
        REFERENCES department_roles (id, organization_id, department_id)
);
CREATE UNIQUE INDEX role_assignments_active_role
    ON role_assignments (department_membership_id, role_id) WHERE revoked_at IS NULL;

CREATE TABLE role_assignment_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    manager_role_id uuid NOT NULL,
    assignable_role_id uuid NOT NULL,
    approved_by uuid NOT NULL REFERENCES users(id),
    revoked_at timestamptz,
    CHECK (manager_role_id <> assignable_role_id),
    FOREIGN KEY (manager_role_id, organization_id, department_id)
        REFERENCES department_roles (id, organization_id, department_id),
    FOREIGN KEY (assignable_role_id, organization_id, department_id)
        REFERENCES department_roles (id, organization_id, department_id)
);
CREATE UNIQUE INDEX role_assignment_rules_active_pair
    ON role_assignment_rules (manager_role_id, assignable_role_id)
    WHERE revoked_at IS NULL;

CREATE TABLE department_join_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL,
    department_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    requested_by uuid NOT NULL,
    requested_role_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    reason text NOT NULL CHECK (length(trim(reason)) > 0),
    reviewed_by uuid REFERENCES users(id),
    reviewed_at timestamptz,
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments (organization_id, id),
    FOREIGN KEY (organization_id, target_user_id)
        REFERENCES users (organization_id, id),
    FOREIGN KEY (organization_id, requested_by)
        REFERENCES users (organization_id, id),
    FOREIGN KEY (requested_role_id, organization_id, department_id)
        REFERENCES department_roles (id, organization_id, department_id)
);
CREATE UNIQUE INDEX department_join_requests_pending_user
    ON department_join_requests (department_id, target_user_id)
    WHERE status = 'pending';

CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid REFERENCES organizations(id),
    department_id uuid,
    actor_user_id uuid NOT NULL REFERENCES users(id),
    support_context_id uuid,
    channel text NOT NULL,
    action text NOT NULL,
    resource_id uuid,
    outcome text NOT NULL,
    reason text,
    correlation_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, department_id)
        REFERENCES departments (organization_id, id),
    FOREIGN KEY (support_context_id, organization_id)
        REFERENCES support_contexts (id, organization_id)
);
CREATE INDEX audit_events_organization_time
    ON audit_events (organization_id, occurred_at DESC);

CREATE FUNCTION reject_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'History is append only';
END;
$$;
CREATE TRIGGER role_versions_immutable BEFORE UPDATE OR DELETE ON role_versions
    FOR EACH ROW EXECUTE FUNCTION reject_history_change();
CREATE TRIGGER role_permissions_immutable BEFORE UPDATE OR DELETE ON role_permissions
    FOR EACH ROW EXECUTE FUNCTION reject_history_change();
CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_history_change();
