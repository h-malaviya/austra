/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");
const departments = [
  { id: "department-1", slug: "inventory", name: "Inventory", archived: false, object_count: 52 },
  { id: "department-2", slug: "retired", name: "Retired", archived: true, object_count: 0 }
];
const users = [
  { id: "admin-1", username: "admin", role: "org_admin", status: "active", must_change_password: false },
  { id: "employee-1", username: "operator", full_name: "Plant Operator",
    email: "operator@example.com", contact_number: "+1 555 0100",
    role: "employee", status: "active", must_change_password: true }
];

function response(data) {
  return { ok: true, status: 200, json: async () => data };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.requestAnimationFrame = callback => callback();
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "platform.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-detail.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-actions.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/,
    "\nwindow.orgTest = { refreshConfig, showDepartments, showSignedOut };"));
  window.fetch = jest.fn(async (url, options = {}) => {
    if (url === "/api/config") return response({
      admin_ready: true, authenticated: true, saas_mode: true, role: "org_admin",
      organization_name: "Acme Works", session_seconds_remaining: 3600,
      connectors: { anthropic: false }
    });
    if (url === "/api/org/departments") return response(departments);
    if (url === "/api/org/users" && options.method === "POST") {
      return response({ id: "employee-2", username: "newhire", temporary_password: "temporary-secret" });
    }
    if (url === "/api/org/users") return response(users);
    if (url.endsWith("/roles")) return response([
      { id: "role-1", slug: "reader", name: "Reader", version: 1,
        permissions: ["records.read"] }
    ]);
    if (url.endsWith("/members")) return response([
      { id: "employee-1", username: "operator", roles: ["Reader"] }
    ]);
    if (url.endsWith("/objects")) return response([{ id: "stock-1", kind: "record" }]);
    return response({});
  });
});

afterEach(() => {
  window.orgTest.showSignedOut(false);
  delete window.orgTest;
});

test("department directory opens a dedicated detail page with working tabs", async () => {
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments();
  expect(window.location.pathname).toBe("/organization/departments");
  expect(document.getElementById("departmentsContent").textContent).toContain("Inventory");
  document.querySelector(".org-name-link").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.pathname).toBe("/organization/departments/department-1");
  expect(document.querySelector(".org-detail-section").textContent).toContain("operator");
  [...document.querySelectorAll(".org-detail-tabs button")]
    .find(button => button.textContent === "Roles").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(document.querySelector(".org-detail-section").textContent).toContain("Read records");
  expect(document.querySelectorAll(".org-page-heading")).toHaveLength(1);
  [...document.querySelectorAll(".org-detail-tabs button")]
    .find(button => button.textContent === "Saved items").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(document.querySelector(".org-detail-section").textContent).toContain("stock-1");
});

test("people page creates an employee and shows a one-time credential", async () => {
  window.history.replaceState({}, "", "/organization/people");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  expect(document.querySelector(".org-page-heading h2").textContent).toBe("People");
  expect(document.querySelector(".org-panel").textContent).toContain("operator");
  [...document.querySelectorAll(".org-page-heading button")]
    .find(button => button.textContent.includes("Create employee")).click();
  const dialog = document.querySelector(".org-modal");
  dialog.querySelector('input[name="username"]').value = "newhire";
  dialog.querySelector('input[name="full_name"]').value = "New Hire";
  dialog.querySelector('input[name="email"]').value = "newhire@example.com";
  dialog.querySelector('input[name="contact_number"]').value = "+1 555 0102";
  dialog.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/users",
    expect.objectContaining({ method: "POST", body: JSON.stringify({
      username: "newhire", full_name: "New Hire", email: "newhire@example.com",
      contact_number: "+1 555 0102"
    }) }));
  expect(document.querySelector(".platform-credential").textContent).toBe("temporary-secret");
});

test("employee profile can be edited and suspended from People", async () => {
  window.history.replaceState({}, "", "/organization/people");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  expect(document.querySelector(".org-panel").textContent).toContain("Plant Operator");
  [...document.querySelectorAll(".org-text-button")]
    .find(button => button.textContent === "Edit").click();
  const form = document.querySelector(".org-modal form");
  form.querySelector('input[name="full_name"]').value = "Senior Operator";
  form.querySelector('select[name="status"]').value = "suspended";
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/users/employee-1",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({
      username: "operator", full_name: "Senior Operator", email: "operator@example.com",
      contact_number: "+1 555 0100", status: "suspended"
    }) }));
});

test("role creation and assignment submit the selected access", async () => {
  window.history.replaceState({}, "", "/organization/departments/department-1");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  [...document.querySelectorAll(".org-detail-tabs button")]
    .find(button => button.textContent === "Roles").click();
  [...document.querySelectorAll(".org-section-head button")]
    .find(button => button.textContent.includes("Create role")).click();
  const roleForm = document.querySelector(".org-modal form");
  roleForm.querySelector('input[name="name"]').value = "Operator";
  roleForm.querySelector('input[name="slug"]').value = "operator";
  roleForm.querySelector('input[value="records.read"]').checked = true;
  roleForm.querySelector('input[name="reason"]').value = "Initial access";
  roleForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/departments/department-1/roles",
    expect.objectContaining({ method: "POST", body: JSON.stringify({
      name: "Operator", slug: "operator", permissions: ["records.read"], reason: "Initial access"
    }) }));
  [...document.querySelectorAll(".org-detail-tabs button")]
    .find(button => button.textContent === "Members").click();
  [...document.querySelectorAll(".org-section-head button")]
    .find(button => button.textContent.includes("Assign role")).click();
  const assignForm = document.querySelector(".org-modal form");
  assignForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith(
    "/api/org/departments/department-1/members/employee-1/roles",
    expect.objectContaining({ method: "POST", body: JSON.stringify({ role_id: "role-1" }) })
  );
});

test("first load shows a skeleton and navigation reuses fresh directory data", async () => {
  await window.orgTest.refreshConfig();
  const originalFetch = window.fetch;
  let completeDepartments;
  window.fetch = jest.fn((url, options) => url === "/api/org/departments"
    ? new Promise(resolve => { completeDepartments = resolve; })
    : originalFetch(url, options));
  const opening = window.orgTest.showDepartments();
  expect(document.querySelector('.loading-state[aria-label="Loading organization data"]')).not.toBeNull();
  expect(document.getElementById("departmentsContent").textContent)
    .not.toContain("Loading organization administration…");
  completeDepartments(response(departments));
  await opening;
  expect(document.querySelector(".loading-state")).toBeNull();
  document.querySelector('a[href="/organization/people"]').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  document.querySelector('a[href="/organization/departments"]').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch.mock.calls.filter(([url]) => url === "/api/org/departments")).toHaveLength(1);
  expect(window.fetch.mock.calls.filter(([url]) => url === "/api/org/users")).toHaveLength(1);
});
