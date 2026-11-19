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
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-capability.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "features/capability-views.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-connections.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-detail.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-actions.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/,
    "\nwindow.orgTest = { refreshConfig, showDepartments, showSignedOut, showChatView, sendMessage, start };"));
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
    if (url === "/api/org/onboarding-cases/current") return response({
      id: "case-1", state: "created", business_description: null, desired_outcome: null
    });
    if (url === "/api/org/onboarding-sources") return response([]);
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

test("new organization starts its guided chat without legacy workspace calls", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/config") return response({
      admin_ready: true, authenticated: true, saas_mode: true, legacy_workspace: false,
      role: "org_admin", organization_name: "New Works", organization_slug: "new-works",
      session_seconds_remaining: 3600, connectors: { anthropic: false },
      guides: { anthropic: { title: "Austra model" } }
    });
    if (url === "/api/org/tour") return response({
      completed: false, organization_name: "New Works", username: "admin",
      about_admin: null, about_organization: null
    });
    return originalFetch(url, options);
  });
  await window.orgTest.start();
  expect(window.location.pathname).toBe("/");
  expect(window.fetch).not.toHaveBeenCalledWith("/api/chats", expect.anything());
  expect(window.fetch).not.toHaveBeenCalledWith("/api/jobs", expect.anything());
  expect(document.getElementById("chatList").hidden).toBe(true);
  expect(document.getElementById("appShell").classList.contains("tour-mode")).toBe(true);
  expect(document.getElementById("messages").textContent).toContain("Anthropic API key");
  document.getElementById("messageInput").value = "What is our policy?";
  await window.orgTest.sendMessage();
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/ask", expect.anything());
  expect(window.fetch).not.toHaveBeenCalledWith("/api/messages/background", expect.anything());
});

test("onboarding interview saves admin answers without loading directory data", async () => {
  window.history.replaceState({}, "", "/organization/onboarding");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  const fields = document.querySelectorAll(".org-onboarding-textarea");
  fields[0].value = "We build pumps";
  fields[1].value = "Track orders";
  [...document.querySelectorAll(".org-onboarding-form button")]
    .find(button => button.textContent === "Save answers").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/onboarding-cases/current/interview",
    expect.objectContaining({ method: "PUT", body: JSON.stringify({
      business_description: "We build pumps", desired_outcome: "Track orders"
    }) }));
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/departments", expect.anything());
});

test("candidate review shows citations and exact hash before confirmation", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/onboarding-cases/current") return response({
      id: "case-1", state: "ready_for_review", business_description: "Pumps", desired_outcome: "Orders"
    });
    if (url === "/api/org/onboarding-cases/case-1/candidate") return response({
      content_hash: "a".repeat(64), changes: ["behavior"],
      behavior: { reads: ["orders"], decisions: ["approval"], actions: [],
        approvers: ["manager"], manual_steps: [], outputs: ["decision"] },
      revisions: [{ kind: "authority", key: "po_limit", rationale: "Signed policy",
        content: { limit: "100 USD" }, valid_from: "2026-01-01T00:00:00Z", valid_to: null,
        sources: [{ name: "SOP", locator: "page 2", claim_id: "claim-1",
          captured_at: "2026-01-01T00:00:00Z" }] }],
      acceptance_examples: [{ category: "boundary", input: "100 USD", expected: "approve" }],
      questions: []
    });
    return originalFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/onboarding");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  const text = document.getElementById("departmentsContent").textContent;
  expect(text).toContain("SOP · page 2");
  expect(text).toContain("100 USD → approve");
  expect(text).toContain("Changed since the previous candidate: behavior");
  [...document.querySelectorAll("button")].find(button =>
    button.textContent === "Confirm this exact candidate").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/onboarding-cases/case-1/candidate/confirm",
    expect.objectContaining({ method: "POST", body: JSON.stringify({ content_hash: "a".repeat(64) }) }));
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

test("released workflow form submits the shared typed command contract", async () => {
  window.history.replaceState({}, "", "/organization/capability");
  Object.defineProperty(window.crypto, "randomUUID", {
    configurable: true, value: jest.fn()
      .mockReturnValueOnce("request-key").mockReturnValueOnce("correlation-key")
  });
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/capability/department-1") return response({
      blueprint_id: "blueprint-1", build_id: "build-1", commands: ["submit_order"],
      projections: [{ key: "journey" }], records: [], complete: true, record_count: 0
    });
    if (url === "/api/org/capability-ui/commands") return response({
      event_id: "event-1", blueprint_id: "blueprint-1", build_id: "build-1",
      record: { external_id: "SO-1", state: "open", revision: 1 }
    });
    return originalFetch(url, options);
  });
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  const form = document.querySelector(".org-capability-form");
  form.querySelector('[name="external_id"]').value = "SO-1";
  form.querySelector('[name="product"]').value = "item";
  form.querySelector('[name="quantity_kg"]').value = "2";
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  const [, options] = window.fetch.mock.calls.find(([url]) => url === "/api/org/capability-ui/commands");
  expect(JSON.parse(options.body)).toEqual({
    action: "submit_order", department_id: "department-1", idempotency_key: "request-key",
    correlation_id: "correlation-key", expected_revisions: {},
    payload: { external_id: "SO-1", product: "item", quantity_kg: "2" }
  });
  expect(document.querySelector(".org-capability-result").textContent).toContain("SO-1: open");
});

test("approved descriptors render a quality queue, dashboard, and typed form", async () => {
  window.history.replaceState({}, "", "/organization/capability");
  const originalFetch = window.fetch;
  const capability = { key: "quality_batches", record_kind: "quality_batch",
    fields: [{ key: "quantity_kg", type: "decimal", unit: "kg" }],
    transitions: [{ key: "receive", from: null, to: "received",
      fields: ["external_id", "quantity_kg"] }] };
  const projections = [
    { key: "receive_form", view: "form", capability: "quality_batches",
      fields: ["external_id", "quantity_kg"], transition: "receive" },
    { key: "quality_queue", view: "queue", capability: "quality_batches",
      fields: ["external_id", "state", "quantity_kg"] },
    { key: "released_weight", view: "dashboard", capability: "quality_batches",
      fields: ["external_id", "quantity_kg"], measure: "quantity_kg", unit: "kg" }
  ];
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/capability/department-1") return response({
      blueprint_id: "blueprint-2", build_id: "build-2", capabilities: [capability], projections
    });
    if (url.endsWith("/projections/quality_queue")) return response({
      descriptor: projections[1], complete: true, record_count: 1,
      records: [{ id: "record-1", external_id: "BATCH-1", revision: 1,
        state: "received", values: { external_id: "BATCH-1", state: "received", quantity_kg: "2" } }]
    });
    if (url.endsWith("/projections/released_weight")) return response({
      descriptor: projections[2], complete: true, record_count: 1, total: "3.5", unit: "kg",
      records: [{ external_id: "BATCH-2", values: { external_id: "BATCH-2", quantity_kg: "3.5" } }]
    });
    if (url === "/api/org/capability-ui/commands") return response({
      event_id: "event-3", blueprint_id: "blueprint-2", build_id: "build-2",
      record: { external_id: "BATCH-3", state: "received", revision: 1 }
    });
    return originalFetch(url, options);
  });
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  expect(document.getElementById("departmentsContent").textContent).toContain("3.5 kg");
  expect(document.getElementById("departmentsContent").textContent).toContain("BATCH-1");
  const form = document.querySelector(".org-capability-form");
  form.querySelector('[name="external_id"]').value = "BATCH-3";
  form.querySelector('[name="quantity_kg"]').value = "1.25";
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  const [, options] = window.fetch.mock.calls.find(([url]) => url === "/api/org/capability-ui/commands");
  expect(JSON.parse(options.body)).toEqual(expect.objectContaining({
    action: "transition_record", department_id: "department-1",
    payload: { capability_key: "quality_batches", transition_key: "receive",
      record_id: null, external_id: "BATCH-3", fields: { quantity_kg: "1.25" } }
  }));
});

test("chat submits a typed capability command through the same contract", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => url === "/api/org/capability-chat/commands"
    ? response({ event_id: "event-2", record: { external_id: "SO-2", state: "open" } })
    : originalFetch(url, options));
  await window.orgTest.refreshConfig();
  const command = { action: "submit_order", department_id: "department-1",
    idempotency_key: "chat-1", correlation_id: "correlation-1", expected_revisions: {},
    payload: { external_id: "SO-2", product: "item", quantity_kg: "2" } };
  document.getElementById("messageInput").value = `/capability ${JSON.stringify(command)}`;
  document.getElementById("sendButton").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/capability-chat/commands",
    expect.objectContaining({ method: "POST", body: JSON.stringify(command) }));
  expect(document.getElementById("messages").textContent).toContain("SO-2: open");
});

test("connection catalog keeps tokens in private forms and shows account repair", async () => {
  const baseFetch = window.fetch;
  window.fetch = jest.fn((url, options = {}) => {
    if (url === "/api/org/connection-catalog") return response([{
      key: "github", label: "GitHub", setup: "private_key", scopes: ["repo"],
      private_fields: ["token"], verification: "Verify your account",
      triggers: [], health_check: "Read account", rate_per_minute: 60,
      error_handling: "Retry after repair",
      revocation: "Revoke the token in GitHub", actions: [{ label: "Create issue", effect: "write" }]
    }]);
    if (url === "/api/org/connections") return response([{
      id: "connection-1", template: "github", account: "fixture-account",
      scopes: ["repo"], actions: [], health: "repair", repair_reason: "Reconnect"
    }]);
    if (url === "/api/org/connections/connection-1" && options.method === "PUT") {
      expect(JSON.parse(options.body)).toEqual({
        template: "github", values: { token: "private-token" }
      });
      return response({ health: "healthy" });
    }
    return baseFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/connections");
  await window.orgTest.refreshConfig();
  await window.orgTest.showDepartments(false);
  const content = document.getElementById("departmentsContent");
  expect(content.textContent).toContain("fixture-account");
  expect(content.textContent).toContain("repo");
  expect(content.textContent).not.toContain("private-token");
  const reconnect = [...content.querySelectorAll("form")].find(form =>
    form.textContent.includes("Reconnect this account"));
  expect(reconnect.elements.token.type).toBe("password");
  reconnect.elements.token.value = "private-token";
  reconnect.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/connections/connection-1",
    expect.objectContaining({ method: "PUT" }));
  expect(content.textContent).not.toContain("private-token");
});
