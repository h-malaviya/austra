const orgAdminState = window.orgAdminState = {
  departments: [], users: [], search: "", detailTab: "members", requestId: 0,
  departmentsAt: 0, usersAt: 0
};
const ORG_CACHE_MS = 30_000;
const orgById = id => document.getElementById(id);
const orgDepartmentsPath = window.orgDepartmentsPath = "/organization/departments";
const orgPeoplePath = "/organization/people";
const orgPermissions = window.orgPermissions = [
  ["chat.use", "Use chat"], ["records.read", "Read records"],
  ["records.write", "Write records"], ["evidence.read", "Read evidence"],
  ["policies.read", "Read policies"], ["policies.draft", "Draft policies"],
  ["workflows.read", "Read workflows"], ["workflows.draft", "Draft workflows"],
  ["workflows.run", "Run workflows"], ["reports.read", "Read reports"],
  ["integrations.use", "Use integrations"],
  ["department.members.manage", "Manage members"],
  ["department.users.create", "Create users"],
  ["department.roles.assign", "Assign roles"]
];

function orgRoute() {
  const path = window.location.pathname;
  if (path === orgPeoplePath) return { page: "people" };
  if (path.startsWith(orgDepartmentsPath + "/")) {
    return { page: "detail", id: path.slice(orgDepartmentsPath.length + 1) };
  }
  return { page: "departments" };
}

function orgButton(parent, label, className, action) {
  const button = add(parent, "button", label, className);
  button.type = "button";
  button.onclick = action;
  return button;
}

function orgLink(parent, label, path, className = "") {
  const link = add(parent, "a", label, className);
  link.href = path;
  link.onclick = event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    window.history.pushState({}, "", path);
    orgAdminState.detailTab = "members";
    window.renderOrganizationManagement().catch(error => orgNotice(error.message, true));
  };
  return link;
}

function orgNotice(message, error = false) {
  const target = orgById("orgNotice");
  if (target) {
    target.textContent = message;
    target.classList.toggle("error", error);
  } else {
    const root = orgById("departmentsContent");
    if (root) {
      root.replaceChildren();
      add(root, "p", message, error ? "org-loading org-error" : "org-loading");
    }
  }
}

function orgPill(parent, active) {
  add(parent, "span", active ? "Active" : "Archived", `org-pill ${active ? "active" : "archived"}`);
}

function orgTable(parent, headers) {
  const scroller = add(parent, "div", "", "org-table-scroll");
  const table = add(scroller, "table", "", "org-table");
  const row = add(add(table, "thead"), "tr");
  headers.forEach(header => add(row, "th", header));
  return add(table, "tbody");
}

function orgStat(parent, label, value, caption) {
  const card = add(parent, "div", "", "org-stat");
  add(card, "span", label);
  add(card, "strong", String(value));
  add(card, "small", caption);
}

function orgShell(route) {
  const root = orgById("departmentsContent");
  root.replaceChildren();
  const header = add(root, "header", "", "org-header");
  const heading = add(header, "div");
  add(heading, "div", "ORGANIZATION ADMINISTRATION", "org-eyebrow");
  add(heading, "h1", "Departments & People");
  add(heading, "p", "Manage your structure, team, and access in one place.");
  const actions = add(header, "div", "", "org-header-actions");
  orgButton(actions, "Refresh", "org-button secondary", () => {
    window.renderOrganizationManagement(true).catch(error => orgNotice(error.message, true));
  });
  orgButton(actions, "← Back to chat", "org-button secondary org-chat-back", () => showChatView());
  const nav = add(root, "nav", "", "org-nav");
  nav.setAttribute("aria-label", "Organization administration");
  orgLink(nav, "Departments", orgDepartmentsPath,
    route.page === "people" ? "" : "active");
  orgLink(nav, "People", orgPeoplePath, route.page === "people" ? "active" : "");
  const content = add(root, "div", "", "org-content");
  const notice = add(content, "p", "", "org-notice");
  notice.id = "orgNotice";
  notice.setAttribute("role", "status");
  return content;
}

function orgDepartmentsPage(content) {
  const departments = orgAdminState.departments;
  const active = departments.filter(item => !item.archived);
  const top = add(content, "div", "", "org-page-heading");
  const title = add(top, "div");
  add(title, "h2", "Departments");
  add(title, "p", "Each department owns its items and access roles.");
  orgButton(top, "+  Create department", "org-button primary", orgCreateDepartment);
  const stats = add(content, "div", "", "org-stats");
  orgStat(stats, "Active departments", active.length, "Currently in use");
  orgStat(stats, "Saved items", active.reduce((sum, item) => sum + item.object_count, 0), "Across active departments");
  orgStat(stats, "Archived", departments.length - active.length, "Retained for audit");
  const panel = add(content, "section", "", "org-panel");
  const panelHead = add(panel, "div", "", "org-panel-head");
  add(panelHead, "h3", "All departments");
  const search = add(panelHead, "input", "", "org-search");
  search.type = "search";
  search.placeholder = "Search departments";
  search.setAttribute("aria-label", "Search departments");
  search.value = orgAdminState.search;
  const body = orgTable(panel, ["DEPARTMENT", "SAVED ITEMS", "STATUS", ""]);
  function rows() {
    body.replaceChildren();
    const query = search.value.trim().toLowerCase();
    orgAdminState.search = search.value;
    const visible = departments.filter(item =>
      [item.name, item.slug].some(value => value.toLowerCase().includes(query)));
    if (!visible.length) {
      const row = add(body, "tr");
      const cell = add(row, "td", "No matching departments", "org-empty");
      cell.colSpan = 4;
    }
    visible.forEach(item => {
      const row = add(body, "tr", "", "org-clickable-row");
      const path = `${orgDepartmentsPath}/${item.id}`;
      row.onclick = event => {
        if (event.target.closest("a, button")) return;
        window.history.pushState({}, "", path);
        window.renderOrganizationManagement().catch(error => orgNotice(error.message, true));
      };
      const name = add(row, "td");
      orgLink(name, item.name, path, "org-name-link");
      add(name, "small", item.slug);
      add(row, "td", String(item.object_count));
      const status = add(row, "td");
      orgPill(status, !item.archived);
      const action = add(row, "td");
      orgLink(action, "Open  →", path, "org-text-link");
    });
  }
  search.oninput = rows;
  rows();
}

function orgPeoplePage(content) {
  const users = orgAdminState.users;
  const top = add(content, "div", "", "org-page-heading");
  const title = add(top, "div");
  add(title, "h2", "People");
  add(title, "p", "Manage employee profiles, sign in, and access.");
  orgButton(top, "+  Create employee", "org-button primary", orgCreateEmployee);
  const stats = add(content, "div", "", "org-stats");
  orgStat(stats, "Employees", users.filter(user => user.role === "employee").length, "Team accounts");
  orgStat(stats, "Administrators", users.filter(user => user.role === "org_admin").length, "Organization access");
  orgStat(stats, "Password change needed", users.filter(user => user.must_change_password).length, "Temporary credentials");
  const panel = add(content, "section", "", "org-panel");
  const panelHead = add(panel, "div", "", "org-panel-head");
  add(panelHead, "h3", "All people");
  add(panelHead, "span", `${users.length} account${users.length === 1 ? "" : "s"}`, "org-muted");
  const body = orgTable(panel, ["PERSON", "EMAIL", "CONTACT", "TYPE", "STATUS", "PASSWORD", "ACTIONS"]);
  if (!users.length) {
    const row = add(body, "tr");
    const cell = add(row, "td", "No people yet", "org-empty");
    cell.colSpan = 7;
  }
  users.forEach(user => {
    const row = add(body, "tr");
    const name = add(row, "td", user.full_name || user.username, "org-person-name");
    add(name, "small", `@${user.username}`);
    add(row, "td", user.email || "—");
    add(row, "td", user.contact_number || "—");
    add(row, "td", user.role === "org_admin" ? "Admin" : "Employee");
    const status = add(row, "td");
    add(status, "span", user.status === "active" ? "Active" : "Suspended",
      `org-pill ${user.status === "active" ? "active" : "archived"}`);
    add(row, "td", user.must_change_password ? "Change required" : "Ready");
    const action = add(row, "td");
    if (user.role === "employee") {
      orgButton(action, "Edit", "org-text-button", () => orgEditEmployee(user));
      orgButton(action, "Reset password", "org-text-button", () => orgResetPassword(user));
    } else {
      add(action, "span", "Managed by platform", "org-muted");
    }
  });
}

window.renderOrganizationManagement = async (refresh = false) => {
  const requestId = ++orgAdminState.requestId;
  const route = orgRoute();
  const now = Date.now();
  const needDepartments = route.page !== "people"
    && (refresh || now - orgAdminState.departmentsAt > ORG_CACHE_MS);
  const needUsers = route.page !== "departments"
    && (refresh || now - orgAdminState.usersAt > ORG_CACHE_MS);
  if (needDepartments || needUsers) {
    const loadingContent = orgShell(route);
    const placeholder = add(loadingContent, "div");
    showLoadingState(placeholder, "Loading organization data", "table");
    try {
      const [departments, users] = await Promise.all([
        needDepartments ? api("/api/org/departments") : Promise.resolve(null),
        needUsers ? api("/api/org/users") : Promise.resolve(null)
      ]);
      if (requestId !== orgAdminState.requestId || orgById("departmentsPage").hidden) return;
      if (departments) {
        orgAdminState.departments = departments;
        orgAdminState.departmentsAt = Date.now();
      }
      if (users) {
        orgAdminState.users = users;
        orgAdminState.usersAt = Date.now();
      }
    } catch (error) {
      placeholder.remove();
      orgNotice(error.message, true);
      throw error;
    }
  }
  if (requestId !== orgAdminState.requestId || orgById("departmentsPage").hidden) return;
  const content = orgShell(route);
  if (route.page === "departments") orgDepartmentsPage(content);
  else if (route.page === "people") orgPeoplePage(content);
  else {
    const department = orgAdminState.departments.find(item => item.id === route.id);
    if (department) {
      const placeholder = add(content, "div");
      showLoadingState(placeholder, "Loading department details", "table");
      try { await orgDepartmentPage(content, department); }
      finally { placeholder.remove(); }
    }
    else add(content, "h2", "Department not found", "org-missing");
  }
};

window.clearOrganizationManagementCache = () => {
  orgAdminState.requestId += 1;
  orgAdminState.departments = [];
  orgAdminState.users = [];
  orgAdminState.departmentsAt = 0;
  orgAdminState.usersAt = 0;
};
