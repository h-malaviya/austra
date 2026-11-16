async function orgDepartmentPage(content, department) {
  const path = `/api/org/departments/${department.id}`;
  const [roles, members, objects] = await Promise.all([
    api(`${path}/roles`), api(`${path}/members`),
    department.archived ? Promise.resolve([]) : api(`${path}/objects`)
  ]);
  const back = add(content, "div", "", "org-breadcrumb");
  orgLink(back, "Departments", window.orgDepartmentsPath);
  add(back, "span", ` / ${department.name}`);
  const top = add(content, "div", "", "org-page-heading");
  const title = add(top, "div");
  add(title, "h2", department.name);
  add(title, "p", `${department.slug} · ${department.archived ? "Archived" : "Active department"}`);
  const actions = add(top, "div", "", "org-heading-actions");
  if (department.archived) {
    orgButton(actions, "Restore department", "org-button primary", async () => {
      try {
        await api(`${path}/restore`, { method: "POST" });
        await window.renderOrganizationManagement(true);
        orgNotice("Department restored. Assignments need to be added again.");
      } catch (error) { orgNotice(error.message, true); }
    });
  } else {
    orgButton(actions, "Rename", "org-button secondary", () => orgRenameDepartment(department));
    orgButton(actions, "Archive", "org-button secondary", () => orgArchiveDepartment(department));
  }
  const stats = add(content, "div", "", "org-stats");
  orgStat(stats, "Saved items", department.object_count, "In this department");
  orgStat(stats, "Members", members.length, "Active memberships");
  orgStat(stats, "Roles", roles.length, "Access profiles");
  const panel = add(content, "section", "", "org-panel org-detail-panel");
  const tabs = add(panel, "div", "", "org-detail-tabs");
  [["members", "Members"], ["roles", "Roles"], ["items", "Saved items"]].forEach(([key, label]) => {
    const button = orgButton(tabs, label, window.orgAdminState.detailTab === key ? "active" : "", () => {
      window.orgAdminState.detailTab = key;
      renderTab();
    });
    button.setAttribute("aria-current", window.orgAdminState.detailTab === key ? "page" : "false");
  });
  const section = add(panel, "div", "", "org-detail-section");
  function renderTab() {
    [...tabs.querySelectorAll("button")].forEach((button, index) => {
      const active = ["members", "roles", "items"][index] === window.orgAdminState.detailTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
    section.replaceChildren();
    if (window.orgAdminState.detailTab === "members") orgMembersSection(section, department, roles, members);
    if (window.orgAdminState.detailTab === "roles") orgRolesSection(section, department, roles);
    if (window.orgAdminState.detailTab === "items") orgItemsSection(section, objects);
  }
  renderTab();
}

function orgMembersSection(section, department, roles, members) {
  const head = add(section, "div", "", "org-section-head");
  const title = add(head, "div");
  add(title, "h3", "Members");
  add(title, "p", "People assigned to this department and their roles.");
  const eligible = window.orgAdminState.users.filter(user => user.role === "employee" && user.status === "active");
  const assign = orgButton(head, "+  Assign role", "org-button primary",
    () => orgAssignRole(department, eligible, roles));
  assign.disabled = department.archived || !eligible.length || !roles.length;
  if (assign.disabled) assign.title = department.archived
    ? "Restore this department first" : "Create a role and an active employee first";
  const body = orgTable(section, ["PERSON", "ASSIGNED ROLES"]);
  if (!members.length) {
    const row = add(body, "tr");
    const cell = add(row, "td", "No members assigned yet", "org-empty");
    cell.colSpan = 2;
  }
  members.forEach(member => {
    const row = add(body, "tr");
    add(row, "td", member.username, "org-person-name");
    add(row, "td", member.roles.join(", ") || "No role assigned");
  });
}

function orgRolesSection(section, department, roles) {
  const head = add(section, "div", "", "org-section-head");
  const title = add(head, "div");
  add(title, "h3", "Roles");
  add(title, "p", "Permission sets for this department.");
  const create = orgButton(head, "+  Create role", "org-button primary", () => orgRoleForm(department));
  create.disabled = department.archived;
  const body = orgTable(section, ["ROLE", "PERMISSIONS", "VERSION", "ACTION"]);
  if (!roles.length) {
    const row = add(body, "tr");
    const cell = add(row, "td", "No roles yet. Create one to assign access.", "org-empty");
    cell.colSpan = 4;
  }
  roles.forEach(role => {
    const row = add(body, "tr");
    const name = add(row, "td", role.name, "org-person-name");
    add(name, "small", role.slug);
    add(row, "td", role.permissions.map(permission =>
      window.orgPermissions.find(([key]) => key === permission)?.[1] || permission).join(", ") || "None");
    add(row, "td", String(role.version));
    const action = add(row, "td");
    if (!department.archived) orgButton(action, "Edit", "org-text-button", () => orgRoleForm(department, role));
  });
}

function orgItemsSection(section, objects) {
  const head = add(section, "div", "", "org-section-head");
  const title = add(head, "div");
  add(title, "h3", "Saved items");
  add(title, "p", "Items currently cataloged under this department.");
  const body = orgTable(section, ["ITEM", "TYPE"]);
  if (!objects.length) {
    const row = add(body, "tr");
    const cell = add(row, "td", "No saved items", "org-empty");
    cell.colSpan = 2;
  }
  objects.forEach(object => {
    const row = add(body, "tr");
    add(row, "td", object.id);
    add(row, "td", object.kind);
  });
}
