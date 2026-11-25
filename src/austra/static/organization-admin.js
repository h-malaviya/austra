const orgAdminState = window.orgAdminState = {
  departments: [], users: [], search: "", detailTab: "members", requestId: 0,
  departmentsAt: 0, usersAt: 0
};
const ORG_CACHE_MS = 30_000;
const orgById = id => document.getElementById(id);
const orgDepartmentsPath = window.orgDepartmentsPath = "/organization/departments";
const orgPeoplePath = "/organization/people";
const orgOnboardingPath = "/organization/onboarding";
const orgWorkflowsPath = "/organization/workflows";
const orgWorkflowReviewsPath = "/organization/workflow-reviews";
const orgConnectionsPath = "/organization/connections";
const orgMonitoringPath = "/organization/monitoring";
const orgPageCopy = {
  departments: ["Departments", "Manage department structure, ownership, and access."],
  detail: ["Department details", "Manage the department’s members, roles, and saved items."],
  people: ["People", "Manage employee accounts, sign-in details, and access."],
  onboarding: ["Business discovery", "Describe your business and manage the sources Austra uses as evidence."],
  reviews: ["Workflow reviews", "Review, confirm, and prepare proposed workflows for release."],
  planning: ["Plan a workflow", "Discuss the process with Austra and submit it for administrator review."],
  capability: ["Workflows", "View released workflows, their steps, and approved rules."],
  connections: ["Connected apps", "Connect accounts and review what each app can do."],
  monitoring: ["Monitoring", "Review operational issues and track workflow health."]
};
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
  if (path === orgOnboardingPath) {
    const params = new URLSearchParams(window.location.search);
    return { page: params.has("workflow") || params.has("purchase_draft") ? "reviews" : "onboarding" };
  }
  if (path === orgWorkflowReviewsPath) return { page: "reviews" };
  if (path === "/organization/workflow-planning") return { page: "planning" };
  if (path === orgWorkflowsPath || path === "/organization/capability") return { page: "capability" };
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
    orgOpenPath(path);
  };
  return link;
}

function orgOpenPath(path) {
  orgAdminState.detailTab = "members";
  if (window.navigateAustra) {
    window.navigateAustra(path).catch(error => orgNotice(error.message, true));
    return;
  }
  window.history.pushState({}, "", path);
  window.renderOrganizationManagement().catch(error => orgNotice(error.message, true));
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

function orgTable(parent, headers, scrollLabel) {
  const scroller = add(parent, "div", "", "org-table-scroll");
  if (scrollLabel) {
    scroller.tabIndex = 0;
    scroller.setAttribute("role", "region");
    scroller.setAttribute("aria-label", scrollLabel);
  }
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
  const restoreRefresh = document.activeElement?.matches(".org-refresh, .org-load-retry");
  root.replaceChildren();
  const header = add(root, "header", "", "org-header");
  const heading = add(header, "div");
  const [title, description] = orgPageCopy[route.page];
  add(heading, "h1", title).id = "organizationPageTitle";
  add(heading, "p", description);
  const actions = add(header, "div", "", "org-header-actions");
  const refresh = orgButton(actions, "Refresh", "org-button secondary org-refresh", () => {
    window.renderOrganizationManagement(true).catch(error => orgNotice(error.message, true));
  });
  if (restoreRefresh) refresh.focus({ preventScroll: true });
  const content = add(root, "div", "", "org-content");
  const notice = add(content, "p", "", "org-notice");
  notice.id = "orgNotice";
  notice.setAttribute("role", "status");
  return content;
}

function orgLoadFailure(parent, message, retry = () => window.renderOrganizationManagement(true)) {
  const panel = add(parent, "section", "", "org-panel org-load-error");
  add(panel, "h2", "Couldn’t load this page");
  add(panel, "p", message).setAttribute("role", "alert");
  orgButton(panel, "Try again", "org-button secondary org-load-retry", retry);
}

function orgDepartmentsPage(content) {
  const departments = orgAdminState.departments;
  const active = departments.filter(item => !item.archived);
  const stats = add(content, "div", "", "org-stats");
  orgStat(stats, "Active departments", active.length, "Currently in use");
  orgStat(stats, "Saved items", active.reduce((sum, item) => sum + item.object_count, 0), "Across active departments");
  orgStat(stats, "Archived", departments.length - active.length, "Retained for audit");
  const panel = add(content, "section", "", "org-panel org-department-directory");
  const panelHead = add(panel, "div", "", "org-panel-head");
  add(panelHead, "h2", "Department directory");
  const tools = add(panelHead, "div", "", "org-directory-tools");
  const search = add(tools, "input", "", "org-search");
  search.type = "search";
  search.placeholder = "Search departments";
  search.setAttribute("aria-label", "Search departments");
  search.value = orgAdminState.search;
  orgButton(tools, "Create department", "org-button primary", orgCreateDepartment);
  const body = orgTable(panel, ["DEPARTMENT", "SAVED ITEMS", "STATUS"]);
  function rows() {
    body.replaceChildren();
    const query = search.value.trim().toLowerCase();
    orgAdminState.search = search.value;
    const visible = departments.filter(item =>
      [item.name, item.slug].some(value => value.toLowerCase().includes(query)));
    if (!visible.length) {
      const row = add(body, "tr");
      const cell = add(row, "td", query ? "No matching departments" : "No departments yet", "org-empty");
      cell.colSpan = 3;
    }
    visible.forEach(item => {
      const row = add(body, "tr", "", "org-clickable-row");
      const path = `${orgDepartmentsPath}/${item.id}`;
      row.onclick = event => {
        if (event.target.closest("a, button")) return;
        orgOpenPath(path);
      };
      const name = add(row, "td");
      orgLink(name, item.name, path, "org-name-link");
      add(name, "small", item.slug);
      add(row, "td", String(item.object_count));
      const status = add(row, "td");
      orgPill(status, !item.archived);
    });
  }
  search.oninput = rows;
  rows();
}

function orgPeoplePage(content) {
  const users = orgAdminState.users;
  const stats = add(content, "div", "", "org-stats");
  orgStat(stats, "Employees", users.filter(user => user.role === "employee").length, "Team accounts");
  orgStat(stats, "Administrators", users.filter(user => user.role === "org_admin").length, "Organization access");
  orgStat(stats, "Password change needed", users.filter(user => user.must_change_password).length, "Temporary credentials");
  const panel = add(content, "section", "", "org-panel");
  const panelHead = add(panel, "div", "", "org-panel-head");
  const title = add(panelHead, "div");
  add(title, "h2", "People directory");
  add(title, "span", `${users.length} account${users.length === 1 ? "" : "s"}`, "org-muted");
  orgButton(panelHead, "Create employee", "org-button primary", orgCreateEmployee);
  const columns = ["PERSON", "EMAIL", "CONTACT", "TYPE", "STATUS", "PASSWORD", "ACTIONS"];
  const body = orgTable(panel, columns, "People directory");
  const table = body.closest("table");
  table.classList.add("org-people-table");
  // Keep table semantics when narrow layouts stack the cells.
  table.setAttribute("role", "table");
  body.setAttribute("role", "rowgroup");
  table.tHead.setAttribute("role", "rowgroup");
  table.tHead.rows[0].setAttribute("role", "row");
  for (const cell of table.tHead.rows[0].cells) cell.setAttribute("role", "columnheader");
  if (!users.length) {
    const row = add(body, "tr");
    const cell = add(row, "td", "No people yet", "org-empty");
    cell.colSpan = 7;
    row.setAttribute("role", "row");
    cell.setAttribute("role", "cell");
  }
  users.forEach(user => {
    const row = add(body, "tr");
    row.setAttribute("role", "row");
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
    Array.from(row.cells).forEach((cell, index) => {
      cell.setAttribute("role", "cell");
      cell.dataset.label = columns[index].toLowerCase();
    });
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
  orgLink(heading, "Workflow reviews", orgWorkflowReviewsPath, "org-button secondary");
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
    add(row, "span", `${source.status} · ${source.review_state === "pending" ? "Needs review" : source.review_state} · ${source.extracted_units}/${source.total_units ?? "?"} sections · ${source.chunk_count} passages`);
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
    orgButton(row, source.review_state === "pending" ? "Include for evidence"
      : source.excluded ? "Include" : "Exclude", "org-button secondary", async () => {
      try {
        await api(`/api/org/onboarding-sources/${source.id}`, { method: "PATCH",
          body: JSON.stringify({ excluded: source.review_state !== "pending" && !source.excluded }) });
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
    if (source.review_state === "pending") {
      orgButton(row, "Reject source", "org-button secondary", () => {
        const existing = row.querySelector(".org-source-rejection textarea");
        if (existing) { existing.focus(); return; }
        const form = add(row, "div", "", "org-source-rejection");
        const label = add(form, "label", `Reason for rejecting ${source.name}`);
        const reason = add(label, "textarea", "", "org-onboarding-textarea");
        reason.maxLength = 500;
        reason.required = true;
        const feedback = add(form, "span", "", "org-form-feedback");
        feedback.setAttribute("role", "status");
        const actions = add(form, "div", "", "org-modal-actions");
        orgButton(actions, "Cancel", "org-button secondary", () => form.remove());
        const confirm = orgButton(actions, "Confirm rejection", "org-button", async () => {
          const explanation = reason.value.trim();
          if (explanation.length < 3) {
            feedback.textContent = "Add a short reason for the review record.";
            reason.focus();
            return;
          }
          confirm.disabled = true;
          try {
            await api(`/api/org/onboarding-sources/${source.id}`, { method: "PATCH",
              body: JSON.stringify({ excluded: true, reason: explanation }) });
            await window.renderOrganizationManagement(true);
          } catch (error) {
            feedback.textContent = error.message;
            confirm.disabled = false;
          }
        });
        reason.focus();
      });
    }
  }
}

async function orgWorkflowReviewsPage(content) {
  orgLink(content, "Manager workflow plans", "/organization/workflow-planning", "org-button secondary");
  const params = new URLSearchParams(window.location.search);
  const draftId = params.get("purchase_draft");
  if (draftId) {
    await orgPreparePurchaseWorkflow(content, draftId);
    return;
  }
  const [caseData, sources] = await Promise.all([
    api("/api/org/onboarding-cases/current"), api("/api/org/onboarding-sources")
  ]);
  const workflows = caseData.workflows?.length ? caseData.workflows : [caseData];
  if (workflows.length === 1) {
    await orgWorkflowReview(content, workflows[0], sources);
    return;
  }
  const label = add(content, "label", "Workflow to review", "org-review-selector");
  const selector = add(label, "select");
  const selected = params.get("workflow");
  for (const item of workflows) add(selector, "option", item.name || "Business workflow").value = item.id;
  if (workflows.some(w => w.id === selected)) selector.value = selected;
  const panel = add(content, "div");
  let selectionRequest = 0;
  const show = async () => {
    const request = ++selectionRequest;
    showLoadingState(panel, "Loading workflow review", "table");
    const current = document.createElement("div");
    const id = selector.value;
    try {
      await orgWorkflowReview(current, workflows.find(w => w.id === id), sources);
      if (request === selectionRequest) panel.replaceChildren(current);
    } catch (error) {
      if (request !== selectionRequest || !panel.isConnected) return;
      panel.replaceChildren();
      orgLoadFailure(panel, error.message, show);
    }
  };
  selector.onchange = () => {
    const url = new URL(window.location.href);
    url.searchParams.set("workflow", selector.value);
    window.history.replaceState({}, "", url);
    show();
  };
  await show();
}

async function orgPreparePurchaseWorkflow(content, draftId) {
  const panel = add(content, "section", "", "org-panel org-onboarding-form");
  add(panel, "h2", "Prepare the sales order and purchase workflow");
  add(panel, "p", "Review an order, reserve verified stock, and request approval for the remaining quantity. Gmail sends the exact approved message. Austra tracks matching supplier replies in the original thread.");
  const departments = (await api("/api/org/departments")).filter(item => !item.archived);
  const dept = add(add(panel, "label", "Workflow department"), "select");
  capabilityOption(dept, "", "Choose a department");
  departments.forEach(item => capabilityOption(dept, item.id, item.name));
  const limit = add(add(panel, "label", "Purchase approval limit (USD)"), "input");
  limit.type = "number";
  limit.min = "0";
  limit.step = "0.01";
  const submit = orgButton(panel, "Prepare workflow for review", "org-button primary", async () => {
    if (!dept.value || !limit.value || !limit.checkValidity()) {
      orgNotice("Choose a department and enter an approval limit.", true);
      return;
    }
    submit.disabled = true;
    try {
      const candidate = await api(`/api/drafts/${draftId}/prepare-purchase-workflow`, {
        method: "POST", body: JSON.stringify({department_id: dept.value, approval_limit_usd: limit.value})
      });
      window.history.replaceState({}, "", orgWorkflowReviewsPath + "?workflow=" + encodeURIComponent(candidate.case_id));
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
    finally { submit.disabled = false; }
  });
}

async function orgWorkflowReview(content, caseData, sources) {
  if (caseData.state === "locked_candidate") {
    const locked = add(content, "section", "", "org-panel org-onboarding-form");
    add(locked, "h3", "Confirmed blueprint candidate");
    add(locked, "p", "The confirmed definition is locked. Build and release status appear below.");
  }
  let candidate;
  try {
    candidate = await api(`/api/org/onboarding-cases/${caseData.id}/candidate`);
  } catch (error) {
    if (!error.message.includes("No candidate")) throw error;
  }
  if (!candidate?.behavior || !candidate?.revisions) {
    const empty = add(content, "section", "", "org-panel org-onboarding-form");
    add(empty, "h2", "No workflow candidate yet");
    add(empty, "p", "Describe a process and add supporting sources to prepare a candidate for review.");
    orgLink(empty, "Open business discovery", orgOnboardingPath, "org-button secondary");
    return;
  }
  const review = add(content, "section", "", "org-panel org-onboarding-form org-workflow-review");
  add(review, "h3", candidate.name || (candidate.revisions.length
    ? "Proposed business behavior" : "Workflow draft from discovery"));
  if (!candidate.revisions.length) add(review, "p",
    "This is a saved proposal for review. It cannot be approved or run until its evidence-backed workflow definition is complete.");
  const identifier = add(review, "details", "", "org-review-technical");
  add(identifier, "summary", "Technical reference");
  if (candidate.id) add(identifier, "p", `Candidate ID: ${candidate.id}`);
  add(identifier, "p", `Candidate SHA-256: ${candidate.content_hash}`);
  if (candidate.changes?.length) add(review, "p",
    `Changed since the previous candidate: ${candidate.changes.join(", ")}`);
  const labels = { reads: "Data read", decisions: "Decisions", actions: "Actions",
    approvers: "Approvers", manual_steps: "Manual steps", outputs: "Example outputs" };
  const facts = add(review, "dl", "", "org-review-facts");
  for (const [field, label] of Object.entries(labels)) {
    const item = add(facts, "div");
    add(item, "dt", label);
    add(item, "dd", (candidate.behavior[field] || []).join("; ") || "None proposed");
  }
  if (candidate.open_decisions?.length) {
    add(review, "h3", "Decisions still open");
    for (const decision of candidate.open_decisions) add(review, "p", decision);
  }
  add(review, "h3", "Why these rules exist");
  for (const {content} of candidate.revisions) {
    if (content.reply_mapping) {
      add(review, "h4", "Reply-to-record mapping");
      add(review, "p", `Updates allowed in: ${content.reply_mapping.states.map(workflowLabel).join(", ")}`);
      for (const field of content.reply_mapping.fields) {
        add(review, "p", `${workflowLabel(field.source)}${field.label ? ` “${field.label}”` : ""} → ${workflowLabel(field.target)}`);
      }
    }
    if (content.email_document) {
      add(review, "h4", "Email document");
      add(review, "p", `${content.email_document.title} (PDF). Includes the record reference and ${content.email_document.fields.map(workflowLabel).join(", ")}.`);
    }
    if (content.automatic_email) {
      const rules = add(review, "details");
      add(rules, "summary", "Approved automatic replies and evidence checks");
      renderAutomaticReplyRules(rules, content.automatic_email);
    }
  }
  for (const item of candidate.revisions) {
    const row = add(review, "div", "", "org-source-row");
    add(row, "strong", `${item.kind}: ${item.key}`);
    add(row, "p", item.rationale);
    for (const [field, value] of Object.entries(item.content)) add(row, "p",
      `${field.replaceAll("_", " ")}: ${typeof value === "object" ? JSON.stringify(value, null, 2) : String(value)}`);
    add(row, "p", `Valid from ${new Date(item.valid_from).toLocaleDateString()}` +
      (item.valid_to ? ` to ${new Date(item.valid_to).toLocaleDateString()}` : ""));
    for (const source of item.sources) add(row, "small",
      `${source.name} · ${source.locator} · captured ${new Date(source.captured_at).toLocaleString()} · claim ${source.claim_id}`);
  }
  add(review, "h3", "Acceptance examples");
  for (const example of candidate.acceptance_examples) {
    add(review, "p", `${example.category}: ${example.input} → ${example.expected}`);
  }
  if (!candidate.revisions.length) {
    add(review, "p", "Choose the supporting passage, resolve any open decisions and define who may run each step.");
    orgWorkflowDefinition(review, caseData, candidate, sources);
    return;
  }
  if (caseData.state !== "locked_candidate" && candidate.suggested_definition) {
    orgWorkflowDefinition(review, caseData, candidate, sources);
  }
  if (caseData.state === "locked_candidate" && caseData.blueprint_id
    && candidate.revisions.some(item => item.kind === "capability")) {
    if (candidate.revisions.some(item => item.key === "so_inventory_po_email")) {
      purchaseWorkflowGraph(review, {commands: Object.keys(capabilityFields), email_jobs: []}, () => {});
    }
    orgBuildControls(review, caseData);
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
          method: "POST", body: JSON.stringify({ question_index: 0, answer: answer.value, content_hash: candidate.content_hash }) });
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

function orgWorkflowDefinition(parent, workflow, candidate, sources) {
  const panel = add(parent, "details");
  add(panel, "summary", "Define workflow and approval rules");
  const files = sources.filter(s => s.kind === "file" && !s.excluded && s.review_state === "included" && s.chunk_count);
  if (!files.length) {
    add(panel, "p", "Include an internal operating document in Sources to define this workflow.");
    return;
  }
  const select = (label, entries) => {
    const field = add(add(panel, "label", label), "select");
    add(field, "option", "Choose…").value = "";
    for (const [value, name] of entries) add(field, "option", name).value = value;
    return field;
  };
  const source = select("Internal document", files.map(s => [s.id, s.name]));
  const passage = select("Passage", []);
  const claim = select("Evidence to confirm", []);
  const clear = element => { element.length = 1; element.value = ""; };
  source.onchange = async () => {
    clear(passage); clear(claim);
    const id = source.value;
    if (!id) return;
    try {
      const rows = await api("/api/org/onboarding-sources/" + id + "/preview");
      if (id === source.value) for (const row of rows) add(passage, "option", row.locator + " · " + row.text).value = row.id;
    } catch (error) { orgNotice(error.message, true); }
  };
  passage.onchange = async () => {
    clear(claim);
    const id = passage.value;
    if (!id) return;
    try {
      const rows = await api("/api/org/semantic-chunks/" + id + "/claims", {method: "POST"});
      if (id === passage.value) for (const row of rows) add(claim, "option", row.proposition).value = row.id;
    } catch (error) { orgNotice(error.message, true); }
  };
  const department = select("Department allowed to run this workflow", [["all", "All authorized departments"]]);
  const approver = select("Who may approve?", [["permission", "Anyone with workflow approval permission"]]);
  let loadingDirectory = false;
  panel.ontoggle = async () => {
    if (!panel.open || loadingDirectory || panel.dataset.directoryReady) return;
    loadingDirectory = true;
    try {
      const [departments, people] = await Promise.all([api("/api/org/departments"), api("/api/org/users")]);
      for (const d of departments.filter(d => !d.archived)) add(department, "option", d.name).value = d.id;
      for (const u of people.filter(u => u.status === "active")) add(approver, "option", u.full_name || u.username).value = u.id;
      panel.dataset.directoryReady = "true";
    } catch (error) { orgNotice(error.message, true); }
    finally { loadingDirectory = false; }
  };
  const rationale = add(add(panel, "label", "How does this evidence authorize these rules?"), "textarea");
  const answers = (candidate.open_decisions || []).map(q => [q, add(add(panel, "label", q), "textarea")]);
  const readDefinition = orgWorkflowFields(panel, candidate.suggested_definition || {}, candidate.email_requirements || []);
  orgButton(panel, "Confirm evidence and prepare candidate", "org-button secondary", async () => {
    try {
      if (!claim.value || !rationale.value.trim()) throw new Error("Choose supporting evidence and give the reason.");
      if (!department.value || !approver.value) throw new Error("Choose the department and approval authority.");
      const definition = readDefinition();
      definition.department_ids = department.value === "all" ? [] : [department.value];
      for (const step of definition.transitions) if (step.permission === "workflows.approve" && approver.value !== "permission")
        step.allowed_user_ids = [approver.value];
      await api("/api/org/onboarding-cases/" + workflow.id + "/candidate/definition", {method: "POST",
        body: JSON.stringify({...definition, claim_id: claim.value, rationale: rationale.value,
          content_hash: candidate.content_hash, resolutions: Object.fromEntries(answers.map(([q, field]) => [q, field.value]))})});
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
}

function orgWorkflowFields(parent, initial, requiredEmail) {
  const layout = add(parent, "div", "", "org-workflow-editor");
  const key = name => name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const keys = input => input.split(",").map(key).filter(Boolean);
  const names = values => (values || []).map(value => value.replaceAll("_", " ")).join(", ");
  const input = (row, label, value = "") => {
    const field = add(add(row, "label", label, "org-capability-field"), "input");
    field.value = value;
    return field;
  };
  const select = (row, label, options, value) => {
    const field = add(add(row, "label", label, "org-capability-field"), "select");
    for (const [option, title] of options) add(field, "option", title).value = option;
    if (value) field.value = value;
    return field;
  };
  const rows = (title, action, initialRows, fields, parent = layout) => {
    const section = add(parent, "fieldset");
    add(section, "legend", title);
    const items = add(section, "div", "", "org-workflow-rows");
    const readers = new Map();
    const append = values => {
      const row = add(items, "div", "", "org-workflow-row");
      readers.set(row, fields(row, values));
      orgButton(row, "Remove", "org-button secondary", () => {
        readers.delete(row);
        row.remove();
      });
    };
    for (const values of initialRows) append(values);
    orgButton(section, action, "org-button secondary", () => append({}));
    return (...args) => Array.from(readers.values(), read => read(...args));
  };
  const states = input(layout, "States (comma-separated)", names(initial.states));
  const recordFields = rows("Record fields", "Add field", initial.fields || [{}], (row, value) => {
    const name = input(row, "Field name", names([value.key || ""]));
    const type = select(row, "Field type", [["text", "Text"], ["decimal", "Number"],
      ["reference", "Reference"]], value.type);
    const unit = input(row, "Unit for a number", value.unit || "");
    return () => ({ key: key(name.value), type: type.value, unit: unit.value.trim() || null });
  });
  const steps = rows("Workflow steps", "Add step", initial.transitions || [{}], (row, value) => {
    const name = input(row, "Step name", names([value.key || ""]));
    const from = input(row, "Starting state (blank for a new record)", names([value.from || ""]));
    const to = input(row, "Next state", names([value.to || ""]));
    const fields = input(row, "Required fields (comma-separated)",
      names((value.fields || []).filter(field => field !== "external_id")));
    const permission = select(row, "Permission", [["workflows.run", "Run workflow"],
      ["workflows.approve", "Approve workflow"]], value.permission);
    const automatic = add(add(row, "label", "Allow this step in approved automatic email rules "), "input");
    automatic.type = "checkbox";
    automatic.checked = Boolean(value.allow_automatic_email);
    const guard = add(row, "details");
    add(guard, "summary", "Numeric condition");
    guard.open = Boolean(value.guard);
    const guardField = input(guard, "Number field", names([value.guard?.field || ""]));
    const op = select(guard, "Comparison", [["lte", "At most"], ["gte", "At least"]], value.guard?.op);
    const threshold = input(guard, "Threshold", value.guard?.value || "");
    return available => {
      const origin = key(from.value);
      const field = key(guardField.value);
      return { key: key(name.value), from: origin || null, to: key(to.value),
        permission: permission.value, ...(automatic.checked ? {allow_automatic_email: true} : {}),
        fields: origin ? keys(fields.value) : ["external_id", ...keys(fields.value)],
        guard: field ? { field, op: op.value, value: threshold.value,
          unit: available.find(item => item.key === field)?.unit || "" } : null };
    };
  });
  const views = rows("Results to show", "Add view", initial.projections || [], (row, value) => {
    const title = input(row, "View title", value.title);
    const kind = select(row, "View type", [["table", "Table"], ["queue", "Queue"], ["chart", "Chart"]],
      value.view);
    const fields = input(row, "Fields to display (comma-separated)", names(value.fields));
    const states = input(row, "States to include (blank for all)", names(value.states));
    const group = input(row, "Chart group field", names([value.group_by || ""]));
    const measure = input(row, "Chart number field (blank to count records)", names([value.measure || ""]));
    return () => ({ key: key(title.value).slice(0, 30), title: title.value,
      view: kind.value, fields: keys(fields.value), states: keys(states.value),
      group_by: kind.value === "chart" ? key(group.value) || null : null,
      measure: kind.value === "chart" ? key(measure.value) || null : null });
  });
  const failure = input(layout, "What happens when a step cannot finish?", initial.failure_path);
  const mapping = add(layout, "details");
  add(mapping, "summary", "Reply-to-record mapping");
  mapping.open = Boolean(initial.reply_mapping) || requiredEmail.includes("reply_mapping");
  const track = add(add(mapping, "label", "Update records from email replies "), "input");
  track.type = "checkbox";
  track.checked = mapping.open;
  add(mapping, "p", "Use “Label: value” in the reply or attachment. Include the approved unit for number fields.");
  const approvedStates = input(mapping, "States where replies can update the record", names(initial.reply_mapping?.states));
  const readMapping = rows("Reply fields", "Add reply field", initial.reply_mapping?.fields || (mapping.open ? [{}] : []), (row, value) => {
    const target = input(row, "Target record field", names([value.target || ""]));
    const source = select(row, "Reply source", [["sender", "Sender"], ["subject", "Subject"],
      ["received_at", "Received at"], ["body_label", "Label in message"], ["attachment_label", "Label in attachment"]], value.source);
    const label = input(row, "Exact reply label", value.label);
    const showLabel = () => { label.parentNode.hidden = !source.value.endsWith("_label"); };
    source.onchange = showLabel;
    showLabel();
    return () => ({target: key(target.value), source: source.value,
      ...(source.value.endsWith("_label") ? {label: label.value.trim()} : {})});
  }, mapping);
  const document = add(layout, "details");
  add(document, "summary", "Document to attach");
  document.open = Boolean(initial.email_document) || requiredEmail.includes("email_document");
  const attach = add(add(document, "label", "Generate and attach a PDF "), "input");
  attach.type = "checkbox";
  attach.checked = document.open;
  const title = input(document, "Document title", initial.email_document?.title);
  const documentFields = input(document, "Record fields to include (comma-separated)", names(initial.email_document?.fields));
  const automation = add(layout, "details");
  add(automation, "summary", "Automatic supplier replies");
  automation.open = Boolean(initial.automatic_email) || requiredEmail.includes("automatic_email");
  add(automation, "p", "Review the evidence checks and the exact outgoing messages. Austra uses the original Gmail thread and approved recipient.");
  const automationRules = add(add(automation, "label", "Automatic reply rules"), "textarea", "org-onboarding-textarea");
  automationRules.value = initial.automatic_email ? JSON.stringify(initial.automatic_email, null, 2) : "";
  return () => {
    const fields = recordFields();
    return { fields, states: keys(states.value), transitions: steps(fields),
      projections: views(), failure_path: failure.value,
      ...(automationRules.value.trim() ? {automatic_email: JSON.parse(automationRules.value)} : {}),
      ...(track.checked ? {reply_mapping: {states: keys(approvedStates.value), fields: readMapping()}} : {}),
      ...(attach.checked ? {email_document: {title: title.value, fields: keys(documentFields.value)}} : {}) };
  };
}

function orgBuildControls(parent, workflow) {
  const path = "/api/org/capability-builds";
  const stage = workflow.build_state;
  const base = path + "/" + workflow.build_id;
  const revision = {expected_revision: workflow.build_revision};
  const action = (label, endpoint, payload) => orgButton(parent, label, "org-button secondary", async () => {
    try {
      await api(endpoint, {method: "POST", body: JSON.stringify(payload())});
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
  if (!stage) {
    const plan = add(add(parent, "label", "Rollback plan"), "textarea");
    action("Build confirmed workflow", path, () => ({blueprint_id: workflow.blueprint_id, rollback_plan: plan.value}));
    return;
  }
  add(parent, "p", "Build status · " + stage.replaceAll("_", " "));
  if (workflow.verification) for (const item of workflow.verification.cases || [])
    add(parent, "p", (item.passed ? "Passed · " : "Failed · ") + item.name);
  if (stage === "building") action("Start verification", base + "/transitions", () => ({...revision, target: "testing"}));
  if (stage === "testing" && !workflow.verification?.passed) action("Verify workflow", base + "/verify", () => ({}));
  if (stage === "testing" && workflow.verification?.passed) action("Open admin preview", base + "/transitions", () => ({...revision, target: "admin_preview"}));
  if (stage === "admin_preview") {
    if (workflow.preview_state !== "accepted") action("Accept preview", base + "/preview", () => ({...revision, accepted: true}));
    else action("Confirm acceptance", base + "/transitions", () => ({...revision, target: "accepted"}));
  }
  if (stage === "accepted") action("Prepare release", base + "/transitions", () => ({...revision, target: "ready_for_release"}));
  if (stage === "ready_for_release") action("Release this workflow", base + "/release", () => revision);
}

window.renderOrganizationManagement = async (refresh = false) => {
  const page = orgById("departmentsPage");
  if (page.hidden) return;
  const requestId = ++orgAdminState.requestId;
  const route = orgRoute();
  const content = orgShell(route);
  const isCurrent = () => requestId === orgAdminState.requestId && !page.hidden;
  const now = Date.now();
  const needDepartments = route.page === "departments" || route.page === "detail"
    ? (refresh || now - orgAdminState.departmentsAt > ORG_CACHE_MS) : false;
  const needUsers = (route.page === "people" || route.page === "detail")
    && (refresh || now - orgAdminState.usersAt > ORG_CACHE_MS);
  const loading = add(content, "div");
  try {
    if (needDepartments || needUsers) {
      showLoadingState(loading, "Loading organization data", "table");
      const [departments, users] = await Promise.all([
        needDepartments ? api("/api/org/departments") : Promise.resolve(null),
        needUsers ? api("/api/org/users") : Promise.resolve(null)
      ]);
      if (!isCurrent()) return;
      if (departments) {
        orgAdminState.departments = departments;
        orgAdminState.departmentsAt = Date.now();
      }
      if (users) {
        orgAdminState.users = users;
        orgAdminState.usersAt = Date.now();
      }
      loading.replaceChildren();
    }
    if (!isCurrent()) return;
    if (route.page === "departments") orgDepartmentsPage(content);
    else if (route.page === "people") orgPeoplePage(content);
    else if (route.page === "detail") {
      const department = orgAdminState.departments.find(item => item.id === route.id);
      if (department) {
        showLoadingState(loading, "Loading department details", "table");
        await orgDepartmentPage(content, department);
      } else {
        add(content, "h2", "Department not found", "org-missing");
        orgLink(content, "Return to departments", orgDepartmentsPath, "org-button secondary");
      }
    } else {
      const title = orgPageCopy[route.page][0];
      showLoadingState(loading, "Loading " + title.toLowerCase(), "table");
      if (route.page === "onboarding") await orgOnboardingPage(content);
      else if (route.page === "planning") await orgWorkflowPlanningPage(content);
      else if (route.page === "reviews") await orgWorkflowReviewsPage(content);
      else if (route.page === "capability") await orgCapabilityPage(content);
      else if (route.page === "connections") await orgConnectionsPage(content);
      else if (route.page === "monitoring") await window.orgMonitoringPage(content);
    }
  } catch (error) {
    if (!isCurrent()) return;
    content.replaceChildren(orgById("orgNotice"));
    orgLoadFailure(content, error.message);
  } finally {
    loading.remove();
  }
};

window.clearOrganizationManagementCache = () => {
  orgAdminState.requestId += 1;
  orgAdminState.departments = [];
  orgAdminState.users = [];
  orgAdminState.departmentsAt = 0;
  orgAdminState.usersAt = 0;
  orgAdminState.lastCapabilityDecision = null;
};
