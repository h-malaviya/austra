const platformState = { organizations: [], search: "" };
const platformById = id => document.getElementById(id);
const platformOrganizationsPath = "/platform/organizations";

function platformLocation() {
  const path = window.location.pathname;
  if (path === platformOrganizationsPath) return { page: "organizations" };
  if (path.startsWith(platformOrganizationsPath + "/")) {
    return { page: "detail", id: path.slice(platformOrganizationsPath.length + 1) };
  }
  return { page: "overview" };
}

function platformNavigate(path) {
  if (window.location.pathname !== path) window.history.pushState({}, "", path);
  renderPlatformOrganizations().catch(error => platformNotice(error.message, true));
}

function platformLink(parent, label, path, className = "") {
  const link = add(parent, "a", label, className);
  link.href = path;
  link.onclick = event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    platformNavigate(path);
  };
  return link;
}

window.addEventListener("popstate", () => {
  if (!platformById("platformRoot")?.hidden) {
    renderPlatformOrganizations().catch(error => platformNotice(error.message, true));
  }
});

function platformButton(parent, label, className, action) {
  const button = add(parent, "button", label, className);
  button.type = "button";
  button.onclick = action;
  return button;
}

function platformField(parent, label, key, value = "", required = false, type = "text") {
  const wrapper = add(parent, "label", "", "platform-field");
  add(wrapper, "span", label);
  if (key === "status") {
    const select = add(wrapper, "select");
    select.name = key;
    ["active", "suspended"].forEach(optionValue => {
      const option = add(select, "option", optionValue === "active" ? "Active" : "Suspended");
      option.value = optionValue;
    });
    select.value = value;
    return select;
  }
  const input = add(wrapper, "input");
  input.name = key;
  input.type = type;
  input.value = value || "";
  input.required = required;
  input.autocomplete = "off";
  if (key === "slug") {
    input.pattern = "[a-z0-9]+(?:-[a-z0-9]+)*";
    input.maxLength = 64;
    input.title = "Use lowercase letters, numbers, and hyphens";
  }
  if (key === "username" || key === "admin_username") {
    input.pattern = "[A-Za-z0-9._-]{3,64}";
    input.title = "Use 3 to 64 letters, numbers, periods, underscores, or hyphens";
  }
  return input;
}

function platformModal(title, description, fields, submitLabel, onSubmit) {
  const previousFocus = document.activeElement;
  const backdrop = add(document.body, "div", "", "platform-backdrop");
  const dialog = add(backdrop, "section", "", "platform-modal");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  const heading = add(dialog, "h2", title);
  heading.id = "platform-modal-title";
  dialog.setAttribute("aria-labelledby", heading.id);
  add(dialog, "p", description, "platform-modal-description");
  const form = add(dialog, "form");
  const grid = add(form, "div", "", "platform-form-grid");
  fields.forEach(field => platformField(grid, ...field));
  const error = add(form, "p", "", "platform-form-error");
  error.setAttribute("role", "alert");
  const actions = add(form, "div", "", "platform-modal-actions");
  platformButton(actions, "Cancel", "platform-button ghost", () => close());
  const submit = add(actions, "button", submitLabel, "platform-button primary");
  submit.type = "submit";
  function close() {
    backdrop.remove();
    if (!document.querySelector(".platform-backdrop")) previousFocus?.focus?.();
  }
  backdrop.onclick = event => { if (event.target === backdrop) close(); };
  dialog.onkeydown = event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll("input, button")];
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    submit.disabled = true;
    error.textContent = "";
    const values = Object.fromEntries(new FormData(form).entries());
    try { await onSubmit(values); close(); }
    catch (problem) { error.textContent = problem.message; }
    finally { submit.disabled = false; }
  };
  form.querySelector("input")?.focus();
}

function platformCredential(result, title) {
  const backdrop = add(document.body, "div", "", "platform-backdrop");
  const dialog = add(backdrop, "section", "", "platform-modal credential-modal");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  add(dialog, "div", "✳", "platform-credential-icon");
  add(dialog, "h2", title);
  add(dialog, "p", `Temporary password for ${result.username || result.admin_username}. It is shown only once. Share it securely; they must change it at sign in.`);
  const credential = add(dialog, "code", result.temporary_password, "platform-credential");
  platformButton(dialog, "Copy password", "platform-button ghost", async () => {
    await navigator.clipboard.writeText(credential.textContent);
  });
  const done = platformButton(dialog, "I saved the password", "platform-button primary", () => backdrop.remove());
  done.focus();
  dialog.onkeydown = event => { if (event.key === "Escape") backdrop.remove(); };
}

function platformStatus(parent, value) {
  add(parent, "span", value === "active" ? "Active" : "Suspended", `platform-pill ${value}`);
}

function platformCell(row, value, className = "") {
  return add(row, "td", value || "—", className);
}

function platformTable(parent, headers) {
  const scroll = add(parent, "div", "", "platform-table-scroll");
  const table = add(scroll, "table", "", "platform-table");
  const head = add(table, "thead");
  const headings = add(head, "tr");
  headers.forEach(label => add(headings, "th", label));
  return add(table, "tbody");
}

function platformOrganizationFields(item = {}) {
  return [
    ["Organization name", "name", item.name, true],
    ["Email", "email", item.email, true, "email"],
    ["Contact number", "contact_number", item.contact_number, true, "tel"],
    ["City", "city", item.city, true],
    ["Country", "country", item.country, true]
  ];
}

function platformAdminFields(item = {}) {
  return [
    ["Username", "username", item.username, true],
    ["Full name", "full_name", item.full_name, true],
    ["Email", "email", item.email, true, "email"],
    ["Contact number", "contact_number", item.contact_number, true, "tel"]
  ];
}

async function platformReload() {
  const root = platformById("platformRoot");
  if (!root.childElementCount) showLoadingState(root, "Loading platform overview", "page");
  platformState.organizations = await api("/api/platform/organizations");
  await renderPlatformOrganizations();
}

async function renderPlatformOrganizations() {
  const location = platformLocation();
  const selected = platformState.organizations.find(item => item.id === location.id);
  const root = platformById("platformRoot");
  root.replaceChildren();
  const rail = add(root, "aside", "", "platform-rail");
  const brand = add(rail, "div", "", "platform-brand");
  add(brand, "span", "A", "platform-brand-mark");
  add(brand, "div", "Austra");
  add(brand.lastChild, "small", "CONTROL CENTER");
  add(rail, "div", "WORKSPACE", "platform-nav-caption");
  platformLink(rail, "▦  Overview", "/", location.page === "overview" ? "platform-nav-active" : "platform-nav-link");
  platformLink(rail, "▤  Organizations", platformOrganizationsPath,
    location.page === "overview" ? "platform-nav-link" : "platform-nav-active");
  const railBottom = add(rail, "div", "", "platform-rail-bottom");
  add(railBottom, "div", "Platform administrator", "platform-account");
  platformButton(railBottom, "Sign out  ↗", "platform-signout", async () => {
    try { await api("/api/logout", { method: "POST" }); showSignedOut(); }
    catch (error) { platformNotice(error.message, true); }
  });

  const main = add(root, "main", "", "platform-main");
  const top = add(main, "header", "", "platform-topbar");
  const breadcrumb = location.page === "overview" ? "Overview"
    : location.page === "organizations" ? "Organizations"
      : selected?.name || "Organization";
  add(top, "div", `Platform / ${breadcrumb}`, "platform-breadcrumb");
  add(top, "div", "ADMIN PORTAL", "platform-topbadge");
  platformButton(top, "Sign out", "platform-mobile-signout", async () => {
    try { await api("/api/logout", { method: "POST" }); showSignedOut(); }
    catch (error) { platformNotice(error.message, true); }
  });
  const mobileNav = add(top, "nav", "", "platform-mobile-nav");
  mobileNav.setAttribute("aria-label", "Platform pages");
  platformLink(mobileNav, "Overview", "/", location.page === "overview" ? "active" : "");
  platformLink(mobileNav, "Organizations", platformOrganizationsPath,
    location.page === "overview" ? "" : "active");
  const content = add(main, "div", "", "platform-content");
  if (location.page === "detail") {
    platformLink(content, "← All organizations", platformOrganizationsPath, "platform-back-link");
    const notice = add(content, "p", "", "platform-notice");
    notice.id = "platformNotice";
    notice.setAttribute("role", "status");
    if (!selected) {
      add(content, "h1", "Organization not found", "platform-missing");
      return;
    }
    const detail = add(content, "section", "", "platform-detail");
    detail.id = "platformDetail";
    showLoadingState(detail, "Loading administrator details", "table");
    await renderPlatformDetail(selected);
    return;
  }
  const intro = add(content, "section", "", "platform-intro");
  const introText = add(intro, "div");
  add(introText, "div", location.page === "overview" ? "PLATFORM OVERVIEW" : "COMPANY DIRECTORY", "platform-eyebrow");
  add(introText, "h1", location.page === "overview" ? "Platform overview" : "Organizations");
  add(introText, "p", location.page === "overview"
    ? "A clear view of companies and administrators across Austra."
    : "Browse company profiles and manage their administrators.");
  if (location.page === "overview") {
    platformLink(intro, "View organizations  →", platformOrganizationsPath, "platform-button primary");
    const stats = add(content, "section", "", "platform-stats");
    const organizations = platformState.organizations;
    [["Organizations", organizations.length, "Across your platform"],
     ["Active", organizations.filter(item => item.status === "active").length, "Ready for work"],
     ["Administrators", organizations.reduce((count, item) => count + item.admin_count, 0), "Across all companies"]]
      .forEach(([label, value, caption]) => {
        const card = add(stats, "div", "", "platform-stat");
        add(card, "span", label);
        add(card, "strong", String(value));
        add(card, "small", caption);
      });
  } else {
    platformButton(intro, "+  Add organization", "platform-button primary", openPlatformCreate);
  }
  const panel = add(content, "section", "", "platform-panel");
  const panelTop = add(panel, "div", "", "platform-panel-top");
  const panelTitle = add(panelTop, "div");
  add(panelTitle, "h2", location.page === "overview" ? "Organizations at a glance" : "All organizations");
  add(panelTitle, "p", location.page === "overview"
    ? "Open a company profile or browse the full directory."
    : "Select a company to view its profile and administrators.");
  if (location.page === "organizations") {
    const search = add(panelTop, "input", "", "platform-search");
    search.type = "search";
    search.placeholder = "Search organizations";
    search.setAttribute("aria-label", "Search organizations");
    search.value = platformState.search;
    search.oninput = () => { platformState.search = search.value; renderPlatformRows(); };
  } else {
    platformLink(panelTop, "View all  →", platformOrganizationsPath, "platform-text-button");
  }
  const tableBody = platformTable(panel, ["ORGANIZATION", "LOCATION", "CONTACT", "ADMINS", "STATUS", ""]);
  tableBody.id = "platformOrganizationRows";
  renderPlatformRows();
  const notice = add(content, "p", "", "platform-notice");
  notice.id = "platformNotice";
  notice.setAttribute("role", "status");
}

function renderPlatformRows() {
  const body = platformById("platformOrganizationRows");
  if (!body) return;
  body.replaceChildren();
  const page = platformLocation().page;
  const query = page === "organizations" ? platformState.search.trim().toLowerCase() : "";
  const visible = platformState.organizations.filter(item =>
    [item.name, item.slug, item.city, item.country, item.email].some(value =>
      (value || "").toLowerCase().includes(query)))
    .slice(0, page === "overview" ? 5 : undefined);
  if (!visible.length) {
    const row = add(body, "tr");
    const cell = platformCell(row, query ? "No matching organizations" : "No organizations yet");
    cell.colSpan = 6;
    cell.className = "platform-empty";
  }
  visible.forEach(item => {
    const row = add(body, "tr");
    row.className = "platform-org-row";
    const path = `${platformOrganizationsPath}/${item.id}`;
    row.onclick = event => {
      if (event.target.closest("a, button")) return;
      platformNavigate(path);
    };
    const name = add(row, "td");
    const label = platformLink(name, item.name, path, "platform-org-name");
    label.title = `Manage ${item.name}`;
    add(name, "small", item.slug + ".localhost");
    platformCell(row, [item.city, item.country].filter(Boolean).join(", "));
    const contact = platformCell(row, item.email);
    add(contact, "small", item.contact_number || "");
    platformCell(row, String(item.admin_count));
    const status = add(row, "td");
    platformStatus(status, item.status);
    const action = add(row, "td");
    platformLink(action, "View  →", path, "platform-row-action");
  });
}

function platformNotice(message, error = false) {
  const notice = platformById("platformNotice");
  if (notice) { notice.textContent = message; notice.classList.toggle("error", error); }
}

function openPlatformCreate() {
  platformModal("Add organization", "Set up a company and its first administrator.", [
    ...platformOrganizationFields(), ["Subdomain slug", "slug", "", true],
    ["Admin username", "admin_username", "", true],
    ["Admin full name", "admin_full_name", "", true],
    ["Admin email", "admin_email", "", true, "email"],
    ["Admin contact number", "admin_contact_number", "", true, "tel"]
  ], "Create organization", async values => {
    const result = await api("/api/platform/organizations", {
      method: "POST", body: JSON.stringify(values)
    });
    await platformReload();
    platformNavigate(`${platformOrganizationsPath}/${result.id}`);
    platformCredential(result, "Organization created");
  });
}

async function renderPlatformDetail(item) {
  const detail = platformById("platformDetail");
  if (!detail) return;
  const admins = await api(`/api/platform/organizations/${item.id}/admins`);
  detail.replaceChildren();
  const heading = add(detail, "div", "", "platform-detail-header");
  const title = add(heading, "div");
  add(title, "div", "ORGANIZATION PROFILE", "platform-eyebrow");
  add(title, "h2", item.name);
  add(title, "p", [item.city, item.country].filter(Boolean).join(", ") || item.slug);
  const details = add(detail, "div", "", "platform-detail-grid");
  [["Subdomain", item.slug + ".localhost"], ["Email", item.email],
   ["Contact number", item.contact_number], ["City", item.city],
   ["Country", item.country], ["Status", item.status]]
    .forEach(([label, value]) => {
      const cell = add(details, "div");
      add(cell, "small", label);
      add(cell, "strong", value || "—");
    });
  const actions = add(detail, "div", "", "platform-detail-actions");
  platformButton(actions, "Edit details", "platform-button ghost", () => {
    platformModal("Edit organization", `Update ${item.name}'s profile.`,
      platformOrganizationFields(item), "Save changes", async values => {
        await api(`/api/platform/organizations/${item.id}`, {
          method: "PATCH", body: JSON.stringify(values)
        });
        await platformReload();
        platformNotice("Organization details saved.");
      });
  });
  platformButton(actions, item.status === "active" ? "Suspend organization" : "Reactivate organization",
    "platform-button ghost", async () => {
      const status = item.status === "active" ? "suspended" : "active";
      if (!await showModal({ title: `${status === "active" ? "Reactivate" : "Suspend"} organization?`,
        message: status === "suspended" ? "This signs out all company users immediately." : "Company users can sign in again.",
        confirmLabel: status === "active" ? "Reactivate" : "Suspend", cancelLabel: "Cancel" })) return;
      try {
        await api(`/api/platform/organizations/${item.id}/status`, {
          method: "PATCH", body: JSON.stringify({ status })
        });
        await platformReload();
      } catch (error) { platformNotice(error.message, true); }
    });
  const link = add(actions, "a", "Open company portal  ↗", "platform-button ghost");
  link.href = organizationUrl(item.slug);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (item.status === "active") {
    platformButton(actions, "Enter support", "platform-button ghost", () => {
      platformModal("Enter support", `View an activity summary for ${item.name}.`,
        [["Reason", "reason", "", true]], "Enter support", async values => {
          await api(`/api/platform/organizations/${item.id}/support-context`, {
            method: "POST", body: JSON.stringify(values)
          });
          const objects = await api("/api/platform/support/objects");
          const summary = add(detail, "div", "", "platform-support-summary");
          add(summary, "h3", "Support context active · 30 minutes");
          objects.forEach(object => add(summary, "p", `${object.department} · ${object.kind}: ${object.count}`));
          platformButton(summary, "Exit support", "platform-button ghost", async () => {
            await api("/api/platform/support-context", { method: "DELETE" });
            summary.remove();
          });
        });
    });
  }
  if (item.test_reset_enabled) {
    platformButton(actions, "Reset test data", "platform-button ghost", () => {
      platformModal("Reset test data", `This clears ${item.name}'s onboarding, chats, documents, workflows, and people. The organization login and Anthropic key remain. Type ${item.slug} to confirm.`,
        [["Subdomain", "confirm_slug", "", true]], "Reset test data", async values => {
          await api(`/api/platform/organizations/${item.id}/test-reset`, {
            method: "POST", body: JSON.stringify(values)
          });
          await platformReload();
          platformNotice("Test data cleared. The organization can start onboarding again.");
        });
    });
  }

  const adminHead = add(detail, "div", "", "platform-admin-head");
  const adminTitle = add(adminHead, "div");
  add(adminTitle, "h3", "Administrators");
  add(adminTitle, "p", `${admins.length} account${admins.length === 1 ? "" : "s"} with company access`);
  platformButton(adminHead, "+  Add admin", "platform-button primary", () => openPlatformAdmin(item));
  const body = platformTable(detail, ["ADMINISTRATOR", "EMAIL", "CONTACT", "STATUS", "ACTIONS"]);
  admins.forEach(admin => {
    const row = add(body, "tr");
    const name = platformCell(row, admin.full_name || admin.username);
    add(name, "small", "@" + admin.username);
    platformCell(row, admin.email);
    platformCell(row, admin.contact_number);
    const status = add(row, "td");
    platformStatus(status, admin.status);
    const action = add(row, "td", "", "platform-admin-actions");
    platformButton(action, "Edit", "platform-text-button", () => openPlatformAdmin(item, admin));
    platformButton(action, "Reset password", "platform-text-button", () => resetPlatformAdmin(item, admin));
  });
  if (!admins.length) {
    const row = add(body, "tr");
    const cell = platformCell(row, "No administrators yet");
    cell.colSpan = 5;
    cell.className = "platform-empty";
  }
}

function openPlatformAdmin(organization, admin = null) {
  const fields = platformAdminFields(admin || {});
  if (admin) fields.push(["Account status", "status", admin.status]);
  platformModal(admin ? "Edit administrator" : "Add administrator",
    admin ? "Update account details or status." : `Create an administrator for ${organization.name}.`,
    fields, admin ? "Save changes" : "Create admin", async values => {
      if (admin && !["active", "suspended"].includes(values.status)) {
        throw new Error("Status must be active or suspended");
      }
      const result = await api(`/api/platform/organizations/${organization.id}/admins${admin ? "/" + admin.id : ""}`, {
        method: admin ? "PATCH" : "POST", body: JSON.stringify(values)
      });
      await platformReload();
      if (!admin) platformCredential(result, "Administrator created");
      else platformNotice("Administrator details saved.");
    });
}

async function resetPlatformAdmin(organization, admin) {
  if (!await showModal({ title: "Reset administrator password?",
    message: `A new temporary password will be issued for ${admin.username}. Their current sessions will end immediately.`,
    confirmLabel: "Reset password", cancelLabel: "Cancel" })) return;
  try {
    const result = await api(`/api/platform/organizations/${organization.id}/admins/${admin.id}/reset-password`, {
      method: "POST"
    });
    platformCredential(result, "Password reset");
  } catch (error) { platformNotice(error.message, true); }
}

window.renderPlatformDashboard = async () => {
  try { await platformReload(); }
  catch (error) { await renderPlatformOrganizations(); platformNotice(error.message, true); }
};
