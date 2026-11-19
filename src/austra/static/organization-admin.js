const orgAdminState = window.orgAdminState = {
  departments: [], users: [], search: "", detailTab: "members", requestId: 0,
  departmentsAt: 0, usersAt: 0
};
const ORG_CACHE_MS = 30_000;
const orgById = id => document.getElementById(id);
const orgDepartmentsPath = window.orgDepartmentsPath = "/organization/departments";
const orgPeoplePath = "/organization/people";
const orgOnboardingPath = "/organization/onboarding";
const orgCapabilityPath = "/organization/capability";
const orgConnectionsPath = "/organization/connections";
const orgMonitoringPath = "/organization/monitoring";
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
  if (path === orgOnboardingPath) return { page: "onboarding" };
  if (path === orgCapabilityPath) return { page: "capability" };
  if (path === orgConnectionsPath) return { page: "connections" };
  if (path === orgMonitoringPath) return { page: "monitoring" };
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
  add(heading, "h1", route.page === "onboarding" ? "Business discovery"
    : route.page === "capability" ? "Operational workflow"
      : route.page === "connections" ? "Connected apps"
        : route.page === "monitoring" ? "Reviews & observability" : "Departments & People");
  add(heading, "p", route.page === "onboarding"
    ? "Describe your business and review the evidence Austra can use."
    : route.page === "capability" ? "Run approved workflows and view current records."
      : route.page === "connections" ? "Choose an app, verify the account, and review what it can do."
        : route.page === "monitoring" ? "See cases that need review and measure operational health."
      : "Manage your structure, team, and access in one place.");
  const actions = add(header, "div", "", "org-header-actions");
  orgButton(actions, "Refresh", "org-button secondary", () => {
    window.renderOrganizationManagement(true).catch(error => orgNotice(error.message, true));
  });
  orgButton(actions, "← Back to chat", "org-button secondary org-chat-back", () => showChatView());
  const nav = add(root, "nav", "", "org-nav");
  nav.setAttribute("aria-label", "Organization administration");
  orgLink(nav, "Departments", orgDepartmentsPath,
    ["departments", "detail"].includes(route.page) ? "active" : "");
  orgLink(nav, "People", orgPeoplePath, route.page === "people" ? "active" : "");
  orgLink(nav, "Onboarding", orgOnboardingPath, route.page === "onboarding" ? "active" : "");
  orgLink(nav, "Workflow", orgCapabilityPath, route.page === "capability" ? "active" : "");
  orgLink(nav, "Connections", orgConnectionsPath, route.page === "connections" ? "active" : "");
  orgLink(nav, "Monitoring", orgMonitoringPath, route.page === "monitoring" ? "active" : "");
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

async function orgOnboardingPage(content) {
  const [caseData, sources] = await Promise.all([
    api("/api/org/onboarding-cases/current"), api("/api/org/onboarding-sources")
  ]);
  const heading = add(content, "div", "", "org-page-heading");
  const title = add(heading, "div");
  add(title, "h2", "Tell Austra about your business");
  add(title, "p", "Your answers and sources are evidence for discovery. They do not activate business rules.");
  const interview = add(content, "form", "", "org-panel org-onboarding-form");
  const description = add(interview, "textarea", "", "org-onboarding-textarea");
  description.placeholder = "What does your organization do? What products, services, and processes matter?";
  description.setAttribute("aria-label", "Business description");
  description.value = caseData.business_description || "";
  const outcome = add(interview, "textarea", "", "org-onboarding-textarea");
  outcome.placeholder = "What would you like Austra to help you achieve?";
  outcome.setAttribute("aria-label", "Desired outcome");
  outcome.value = caseData.desired_outcome || "";
  const save = orgButton(interview, "Save answers", "org-button primary", async () => {
    try {
      await api("/api/org/onboarding-cases/current/interview", { method: "PUT",
        body: JSON.stringify({ business_description: description.value, desired_outcome: outcome.value }) });
      orgNotice("Answers saved.");
    } catch (error) { orgNotice(error.message, true); }
  });
  save.type = "button";
  const intake = add(content, "section", "", "org-panel org-onboarding-form");
  add(intake, "h3", "Add sources");
  add(intake, "p", "Upload TXT, PDF, DOCX, CSV, or XLSX. Originals stay private and each changed copy gets a new revision.");
  const picker = add(intake, "input");
  picker.type = "file";
  picker.accept = ".txt,.pdf,.docx,.csv,.xlsx";
  picker.setAttribute("aria-label", "Choose evidence file");
  const linkedinExport = add(intake, "label", "LinkedIn Page export");
  const exportCheck = add(linkedinExport, "input");
  exportCheck.type = "checkbox";
  orgButton(intake, "Upload file", "org-button secondary", async () => {
    if (!picker.files.length) return orgNotice("Choose a file first.", true);
    const body = new FormData();
    body.append("upload", picker.files[0]);
    try {
      const response = await fetch(`/api/org/onboarding-sources/files?linkedin=${exportCheck.checked}`, {
        method: "POST", credentials: "same-origin", headers: { "X-Austra-Request": "1" }, body
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Upload failed");
      orgNotice(result.changed ? "File saved. Extraction can resume below." : "This file is unchanged.");
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
  const website = add(intake, "input", "", "org-search");
  website.type = "url";
  website.placeholder = "https://your-public-website.example";
  website.setAttribute("aria-label", "Organization website");
  orgButton(intake, "Fetch website", "org-button secondary", async () => {
    try {
      const result = await api("/api/org/onboarding-sources/website", {
        method: "POST", body: JSON.stringify({ url: website.value }) });
      orgNotice(`${result.pages.length} website page(s) captured.`);
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
  add(intake, "p", "For LinkedIn, use an authorized Page export or paste Page text. Austra does not access LinkedIn without a supported connection.");
  const manual = add(intake, "textarea", "", "org-onboarding-textarea");
  manual.placeholder = "Optional LinkedIn Page text you are authorized to share";
  manual.setAttribute("aria-label", "LinkedIn Page text");
  orgButton(intake, "Save LinkedIn text", "org-button secondary", async () => {
    try {
      await api("/api/org/onboarding-sources/linkedin-text", {
        method: "POST", body: JSON.stringify({ text: manual.value }) });
      orgNotice("LinkedIn Page text saved.");
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
  const sourcePanel = add(content, "section", "", "org-panel org-onboarding-form");
  add(sourcePanel, "h3", "Source coverage");
  if (!sources.length) add(sourcePanel, "p", "No sources yet.");
  for (const source of sources) {
    const row = add(sourcePanel, "div", "", "org-source-row");
    add(row, "strong", `${source.name} · revision ${source.revision}`);
    add(row, "span", `${source.status} · ${source.extracted_units}/${source.total_units ?? "?"} sections · ${source.chunk_count} passages`);
    add(row, "small", `Captured ${new Date(source.captured_at).toLocaleString()} · SHA-256 ${source.sha256}`);
    if (source.status !== "complete") orgButton(row, "Resume extraction", "org-button secondary", async () => {
      try {
        let result;
        do {
          result = await api(`/api/org/onboarding-sources/revisions/${source.revision_id}/process`, { method: "POST" });
          orgNotice(`${source.name}: ${result.extracted_units} sections extracted`);
        } while (result.state === "pending");
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
    orgButton(row, "Preview", "org-button secondary", async () => {
      try {
        const preview = await api(`/api/org/onboarding-sources/${source.id}/preview`);
        const area = add(row, "div", "", "org-source-preview");
        for (const item of preview) add(area, "p", `${item.locator}: ${item.text}`);
      } catch (error) { orgNotice(error.message, true); }
    });
    orgButton(row, source.excluded ? "Include" : "Exclude", "org-button secondary", async () => {
      try {
        await api(`/api/org/onboarding-sources/${source.id}`, { method: "PATCH",
          body: JSON.stringify({ excluded: !source.excluded }) });
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
  }
  if (caseData.state === "locked_candidate") {
    const locked = add(content, "section", "", "org-panel org-onboarding-form");
    add(locked, "h3", "Confirmed blueprint candidate");
    add(locked, "p", "This candidate is locked for build review. It has not changed your active business rules.");
  }
  let candidate;
  try {
    candidate = await api(`/api/org/onboarding-cases/${caseData.id}/candidate`);
  } catch (error) {
    if (!error.message.includes("No candidate")) throw error;
  }
  if (!candidate?.behavior || !candidate?.revisions) return;
  const review = add(content, "section", "", "org-panel org-onboarding-form");
  add(review, "h3", "Proposed business behavior");
  add(review, "p", `Candidate SHA-256: ${candidate.content_hash}`);
  if (candidate.changes?.length) add(review, "p",
    `Changed since the previous candidate: ${candidate.changes.join(", ")}`);
  const labels = { reads: "Data read", decisions: "Decisions", actions: "Actions",
    approvers: "Approvers", manual_steps: "Manual steps", outputs: "Example outputs" };
  for (const [field, label] of Object.entries(labels)) {
    add(review, "strong", label);
    add(review, "p", (candidate.behavior[field] || []).join("; ") || "None proposed");
  }
  add(review, "h3", "Why these rules exist");
  for (const item of candidate.revisions) {
    const row = add(review, "div", "", "org-source-row");
    add(row, "strong", `${item.kind}: ${item.key}`);
    add(row, "p", item.rationale);
    for (const [field, value] of Object.entries(item.content)) add(row, "p",
      `${field.replaceAll("_", " ")}: ${Array.isArray(value) ? value.join("; ") : String(value)}`);
    add(row, "p", `Valid from ${new Date(item.valid_from).toLocaleDateString()}` +
      (item.valid_to ? ` to ${new Date(item.valid_to).toLocaleDateString()}` : ""));
    for (const source of item.sources) add(row, "small",
      `${source.name} · ${source.locator} · captured ${new Date(source.captured_at).toLocaleString()} · claim ${source.claim_id}`);
  }
  add(review, "h3", "Acceptance examples");
  for (const example of candidate.acceptance_examples) {
    add(review, "p", `${example.category}: ${example.input} → ${example.expected}`);
  }
  if (candidate.questions.length) {
    const topic = candidate.questions[0].topic;
    add(review, "h3", `Clarify ${topic.replaceAll("_", " ")}`);
    for (const question of candidate.questions.filter(item => item.topic === topic)) {
      add(review, "p", question.question);
      for (const source of question.citations) add(review, "small",
        `${source.name} · ${source.locator} · ${source.claim_id}`);
    }
    for (const item of candidate.clarifications || []) add(review, "p",
      `Your answer to ${item.question}: ${item.answer}`);
    const answer = add(review, "textarea", "", "org-onboarding-textarea");
    answer.placeholder = "Answer this question for the next candidate revision";
    answer.setAttribute("aria-label", "Clarification answer");
    orgButton(review, "Save clarification", "org-button secondary", async () => {
      try {
        await api(`/api/org/onboarding-cases/${caseData.id}/candidate/answers`, {
          method: "POST", body: JSON.stringify({ question_index: 0, answer: answer.value }) });
        orgNotice("Answer recorded in a new candidate. The underlying rule still needs review.");
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
    add(review, "p", "Resolve the cited claims or missing details, then submit a revised candidate for review.");
  } else if (caseData.state !== "locked_candidate") {
    orgButton(review, "Confirm this exact candidate", "org-button primary", async () => {
      try {
        await api(`/api/org/onboarding-cases/${caseData.id}/candidate/confirm`, {
          method: "POST", body: JSON.stringify({ content_hash: candidate.content_hash }) });
        orgNotice("Blueprint candidate confirmed and locked for build review.");
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
  }
}

window.renderOrganizationManagement = async (refresh = false) => {
  const requestId = ++orgAdminState.requestId;
  const route = orgRoute();
  const now = Date.now();
  const needDepartments = route.page === "departments" || route.page === "detail"
    ? (refresh || now - orgAdminState.departmentsAt > ORG_CACHE_MS) : false;
  const needUsers = (route.page === "people" || route.page === "detail")
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
  else if (route.page === "onboarding") await orgOnboardingPage(content);
  else if (route.page === "capability") await orgCapabilityPage(content);
  else if (route.page === "connections") await orgConnectionsPage(content);
  else if (route.page === "monitoring") await window.orgMonitoringPage(content);
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
