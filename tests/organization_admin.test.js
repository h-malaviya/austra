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
  window.eval(fs.readFileSync(path.join(staticRoot, "workflow-planning.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-capability.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "workflow-chat.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "workflow-alerts.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "features/capability-views.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-connections.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-detail.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-actions.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/,
    "\nwindow.orgTest = { refreshConfig, renderAustraRoute, navigateAustra, showSignedOut, showChatView, sendMessage, renderChat, start };"));
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
  await window.orgTest.navigateAustra("/organization/departments");
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
  await window.orgTest.renderAustraRoute();
  expect(document.querySelector(".org-header h1").textContent).toBe("Business discovery");
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/onboarding-cases/case-1/candidate", expect.anything());
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

test("saved onboarding link opens review directly after sign in", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/config") return response({
      admin_ready: true, authenticated: true, saas_mode: true, legacy_workspace: false,
      role: "org_admin", organization_name: "Acme Works",
      session_seconds_remaining: 3600, connectors: { anthropic: false },
      guides: { anthropic: { title: "Austra model" } }
    });
    if (url === "/api/org/tour") return response({ completed: true, revision: 2, messages: [] });
    if (url === "/api/org/chats") return response([]);
    return originalFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/onboarding");
  await window.orgTest.start();
  expect(window.location.pathname).toBe("/organization/onboarding");
  expect(document.getElementById("departmentsPage").hidden).toBe(false);
  expect(document.getElementById("departmentsContent").textContent).toContain("Business discovery");
});

test("a legacy workflow review link shows citations and exact hash before confirmation", async () => {
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
  window.history.replaceState({}, "", "/organization/onboarding?workflow=case-1");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  expect(document.querySelector(".org-header h1").textContent).toBe("Workflow reviews");
  expect(document.querySelector('[aria-label="Business description"]')).toBeNull();
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

test("discovery draft shows steps and open decisions without an approval button", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/onboarding-cases/current") return response({
      id: "case-1", state: "clarifying", business_description: "Client enquiries",
      desired_outcome: "Shared visibility"
    });
    if (url === "/api/org/onboarding-cases/case-1/candidate") return response({
      id: "draft-1", name: "Client enquiry handoff", content_hash: "b".repeat(64), revisions: [],
      behavior: { manual_steps: ["Harshil decides qualification"], actions: [] },
      open_decisions: ["What counts as signed?"],
      acceptance_examples: [{ category: "normal", input: "New enquiry", expected: "Review" }],
      questions: [{ topic: "exception", question: "Missing tenant graph member", citations: [] }]
    });
    return originalFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/workflow-reviews");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  const text = document.getElementById("departmentsContent").textContent;
  expect(text).toContain("Client enquiry handoff");
  expect(text).toContain("Candidate ID: draft-1");
  expect(text).toContain("Harshil decides qualification");
  expect(text).toContain("What counts as signed?");
  expect(text).not.toContain("Missing tenant graph member");
  expect(text).not.toContain("Confirm this exact candidate");
});

test("switching workflow reviews confirms only the selected candidate", async () => {
  const records = [{id: "one", name: "Lot intake", state: "ready_for_review"},
    {id: "two", name: "Deviation", state: "ready_for_review"}];
  const original = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/onboarding-cases/current") return response({id: "one", workflows: records});
    const record = records.find(item => url === "/api/org/onboarding-cases/" + item.id + "/candidate");
    if (record) return response({id: record.id, name: record.name, content_hash: record.id.repeat(3),
      behavior: {}, revisions: [{kind: "capability", key: "flow", content: {}, sources: []}],
      acceptance_examples: [], questions: []});
    return original(url, options);
  });
  window.history.replaceState({}, "", "/organization/workflow-reviews");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  const selector = [...document.querySelectorAll("label")].find(label =>
    label.textContent.startsWith("Workflow to review")).querySelector("select");
  selector.value = "two";
  selector.dispatchEvent(new Event("change"));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.search).toBe("?workflow=two");
  const confirm = [...document.querySelectorAll("button")].find(b => b.textContent === "Confirm this exact candidate");
  confirm.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/onboarding-cases/two/candidate/confirm",
    expect.objectContaining({method: "POST", body: JSON.stringify({content_hash: "twotwotwo"})}));
});

test("pending source can be rejected with a recorded reason", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/onboarding-sources") return response([{
      id: "source-1", name: "Unrelated inventory", revision: 1, status: "complete",
      review_state: "pending", excluded: false, extracted_units: 1, total_units: 1,
      chunk_count: 1, captured_at: "2026-11-20T00:00:00Z", sha256: "a".repeat(64)
    }]);
    if (url === "/api/org/onboarding-sources/source-1") return response({
      id: "source-1", excluded: true, review_state: "excluded"
    });
    return originalFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/onboarding");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  const button = [...document.querySelectorAll("button")]
    .find(item => item.textContent === "Reject source");
  button.click();
  const reason = document.querySelector(".org-source-rejection textarea");
  const confirm = [...document.querySelectorAll(".org-source-rejection button")]
    .find(item => item.textContent === "Confirm rejection");
  confirm.click();
  expect(document.querySelector(".org-source-rejection .org-form-feedback").textContent)
    .toContain("Add a short reason");
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/onboarding-sources/source-1",
    expect.objectContaining({ method: "PATCH" }));
  reason.value = "Unrelated to our business";
  confirm.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/onboarding-sources/source-1",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({
      excluded: true, reason: "Unrelated to our business"
    }) }));
});

test("pending source can be included without a rejection reason", async () => {
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/onboarding-sources") return response([{
      id: "source-2", name: "Working note", revision: 1, status: "complete",
      review_state: "pending", excluded: false, extracted_units: 1, total_units: 1,
      chunk_count: 1, captured_at: "2026-11-20T00:00:00Z", sha256: "b".repeat(64)
    }]);
    if (url === "/api/org/onboarding-sources/source-2") return response({
      id: "source-2", excluded: false, review_state: "included"
    });
    return originalFetch(url, options);
  });
  window.history.replaceState({}, "", "/organization/onboarding");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  [...document.querySelectorAll("button")]
    .find(button => button.textContent === "Include for evidence").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith("/api/org/onboarding-sources/source-2",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ excluded: false }) }));
});

test("people page creates an employee and shows a one-time credential", async () => {
  window.history.replaceState({}, "", "/organization/people");
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  expect(document.querySelector(".org-header h1").textContent).toBe("People");
  expect(document.querySelector(".org-panel").textContent).toContain("operator");
  [...document.querySelectorAll(".org-panel-head button")]
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
  await window.orgTest.renderAustraRoute();
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
  await window.orgTest.renderAustraRoute();
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
  const opening = window.orgTest.navigateAustra("/organization/departments");
  expect(document.querySelector('.loading-state[aria-label="Loading organization data"]')).not.toBeNull();
  expect(document.getElementById("departmentsContent").textContent)
    .not.toContain("Loading organization administration…");
  completeDepartments(response(departments));
  await opening;
  expect(document.querySelector(".loading-state")).toBeNull();
  document.getElementById("peopleButton").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  document.getElementById("departmentsButton").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch.mock.calls.filter(([url]) => url === "/api/org/departments")).toHaveLength(1);
  expect(window.fetch.mock.calls.filter(([url]) => url === "/api/org/users")).toHaveLength(1);
});

test("People recovers from a failed directory request without losing keyboard focus", async () => {
  await window.orgTest.refreshConfig();
  const originalFetch = window.fetch;
  let fail = true;
  window.fetch = jest.fn((url, options) => {
    if (url !== "/api/org/users" || !fail) return originalFetch(url, options);
    fail = false;
    return { ok: false, status: 503, json: async () => ({ detail: "Directory unavailable" }) };
  });

  await window.orgTest.navigateAustra("/organization/people");

  expect(document.querySelector(".org-load-error [role=alert]").textContent).toBe("Directory unavailable");
  const retry = document.querySelector(".org-load-retry");
  retry.focus();
  retry.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(document.querySelector(".org-load-error")).toBeNull();
  expect(document.querySelector(".org-panel").textContent).toContain("Plant Operator");
  expect(document.activeElement).toBe(document.querySelector(".org-refresh"));
});

test("a failed Departments request does not change the People page after navigation", async () => {
  await window.orgTest.refreshConfig();
  const originalFetch = window.fetch;
  let finishDepartments;
  window.fetch = jest.fn((url, options) => url === "/api/org/departments"
    ? new Promise(resolve => { finishDepartments = resolve; })
    : originalFetch(url, options));

  const opening = window.orgTest.navigateAustra("/organization/departments");
  await window.orgTest.navigateAustra("/organization/people");
  finishDepartments({ ok: false, status: 503, json: async () => ({ detail: "Departments unavailable" }) });
  await opening;

  expect(document.querySelector(".org-header h1").textContent).toBe("People");
  expect(document.querySelector(".org-panel").textContent).toContain("Plant Operator");
  expect(document.getElementById("departmentsContent").textContent).not.toContain("Departments unavailable");
});

const demoCatalog = [{ name: "Demo Purchase", department: "Inventory",
  department_id: "department-1", build_id: "build-1" }];

async function reopenWorkflowChat(step) {
  await window.orgTest.refreshConfig();
  window.orgTest.renderChat({chat: {id: "chat-1", title: "Run Demo Purchase workflow", messages: [],
    workflow: {...demoCatalog[0], step}}});
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

test("a chat workflow form submits a pinned typed command", async () => {
  expect(window.wantsWorkflowChat("Run the Demo Purchase workflow in Inventory.")).toBe(true);
  expect(window.wantsWorkflowChat("Please draft the Demo Purchase workflow.")).toBe(false);
  Object.defineProperty(window.crypto, "randomUUID", {
    configurable: true, value: jest.fn()
      .mockReturnValueOnce("request-key").mockReturnValueOnce("correlation-key")
  });
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/released-workflows") return response(demoCatalog);
    if (url.startsWith("/api/org/capability/department-1")) return response({
      blueprint_id: "blueprint-1", build_id: "build-1", commands: ["submit_order"],
      projections: [{ key: "journey" }], records: [], complete: true, record_count: 0
    });
    if (url === "/api/org/capability-chat/commands") return response({
      event_id: "event-1", blueprint_id: "blueprint-1", build_id: "build-1",
      record: { external_id: "SO-1", state: "open", revision: 1 }
    });
    return originalFetch(url, options);
  });
  await reopenWorkflowChat("submit_order");
  await new Promise(resolve => setTimeout(resolve, 0));
  const form = document.querySelector(".org-capability-form");
  expect(document.querySelector(".workflow-chat-notice")?.textContent).toBe("");
  form.querySelector('[name="external_id"]').value = "SO-1";
  form.querySelector('[name="product"]').value = "item";
  form.querySelector('[name="quantity_kg"]').value = "2";
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await new Promise(resolve => setTimeout(resolve, 0));
  const [, options] = window.fetch.mock.calls.find(([url]) => url === "/api/org/capability-chat/commands");
  expect(JSON.parse(options.body)).toEqual({
    action: "submit_order", department_id: "department-1", build_id: "build-1", idempotency_key: "request-key",
    correlation_id: "correlation-key", expected_revisions: {},
    payload: { external_id: "SO-1", product: "item", quantity_kg: "2" }
  });
  expect(document.querySelector(".org-capability-result").textContent).toContain("SO-1: open");
});

test("the chat refreshes the thread timeline after the first Gmail reply", async () => {
  jest.useFakeTimers();
  const visibility = jest.spyOn(document, "hidden", "get").mockReturnValue(false);
  try {
    const originalFetch = window.fetch;
    const reply = { message_id: "reply-1", subject: "Quantity confirmed",
      from: "supplier@example.com", received_ms: 1730000000000 };
    const next = { ...reply, message_id: "reply-2", subject: "Delivery date confirmed" };
    let refresh = 0;
    window.fetch = jest.fn((url, options) => {
      if (url === "/api/org/released-workflows") return response(demoCatalog);
      if (url.startsWith("/api/org/capability/department-1")) return response({
        blueprint_id: "blueprint-1", build_id: "build-1", commands: ["submit_order"],
        projections: [{ key: "journey" }], records: [], complete: true, record_count: 0,
        email_jobs: [{ id: "job-1", reference: "Email-1", state: "delivered", subject: "Purchase request",
          recipient: "supplier@example.com", tracking: "tracking", replies: [reply] }]
      });
      if (url.endsWith("/job-1/replies") && ++refresh === 1) return response({
        replies: [reply, next], tracking: "retrying",
        recovery: {"reply-2": {state: "retrying"}}
      });
      if (url.endsWith("/job-1/replies")) return response({
        replies: [reply, next], tracking: "tracking",
        tracked_record: {reference: "SO-1", state: "closed", revision: 3},
        recovery: {"reply-2": {state: "recovered"}},
        automatic_actions: {"reply-2": {state: "sent", findings: ["Confirm edge crush rating."]}},
        evidence: {"reply-2": {body: "Lead time: 7 days", attachments: [
          {name: "offer.docx", status: "extracted", excerpts: [{text: "Quote reference: Q-12"}]}
        ]}}
      });
      return originalFetch(url, options);
    });
    await reopenWorkflowChat("email_jobs");
    const panel = document.querySelector("[data-workflow-email]");
    expect(panel.textContent).toContain("1 reply received");
    await jest.advanceTimersByTimeAsync(5000);
    expect(panel.textContent).toContain("2 replies received");
    expect([...panel.querySelectorAll("li > strong")].map(item => item.textContent)).toEqual([
      reply.subject, next.subject,
    ]);
    expect(panel.textContent).toContain("Austra will retry processing its content automatically.");
    await jest.advanceTimersByTimeAsync(5000);
    expect(panel.textContent).toContain("Austra recovered this reply’s content");
    expect(panel.textContent).toContain("Austra replied in this Gmail thread.");
    expect(panel.textContent).toContain("SO-1 · closed · Revision 3");
    expect(panel.textContent).toContain("Confirm edge crush rating.");
    expect(window.fetch.mock.calls.some(([url]) => url.endsWith("/check-reply"))).toBe(false);
    expect(panel.querySelector("details").textContent).toContain("offer.docx");
  } finally {
    visibility.mockRestore();
    jest.clearAllTimers();
    jest.useRealTimers();
  }
});

test("a department manager can open a saved workflow conversation and ask Austra a question", async () => {
  const original = window.fetch;
  const initial = {id: "plan-1", revision: 1, messages: [], unresolved: [], proposal: null};
  window.fetch = jest.fn(async (url, options) => {
    if (url === "/api/config") return response({admin_ready: true, authenticated: true, saas_mode: true,
      role: "employee", organization_name: "Acme Works", session_seconds_remaining: 3600,
      connectors: {anthropic: true}});
    if (url === "/api/org/workflow-plans") return response({departments: [{id: "department-1", name: "Inventory"}],
      plans: [{id: "plan-1", name: "New workflow", department: "Inventory", owner: "Riya", mine: true}]});
    if (url === "/api/org/workflow-plans/plan-1") return response(initial);
    if (url === "/api/org/workflow-plans/plan-1/messages") {
      expect(JSON.parse(options.body)).toEqual({revision: 1, body: "Review incoming carton quotes."});
      return response({...initial, revision: 2, messages: [
        {role: "user", body: "Review incoming carton quotes."},
        {role: "assistant", body: "Who may approve the quote?"}
      ]});
    }
    return original(url, options);
  });
  await window.orgTest.refreshConfig();
  await window.orgTest.navigateAustra("/organization/workflow-planning?plan=plan-1");
  const view = document.getElementById("departmentsContent");
  view.querySelector("textarea").value = "Review incoming carton quotes.";
  await [...view.querySelectorAll("button")].find(button => button.textContent === "Send message").onclick();
  expect(view.querySelector('[role="log"]').textContent).toContain("Who may approve the quote?");
  expect(view.textContent).not.toContain("Open as workflow candidate");
});

test("chat prepares a record email through A2UI and previews the exact PDF before sending", async () => {
  const originalFetch = window.fetch;
  const job = {id: "job-1", reference: "DSP-001", recipient: "carrier@example.com",
    subject: "Dispatch request", state: "pending"};
  let prepared = false;
  let input;
  const originalA2UI = window.AustraA2UI;
  window.AustraA2UI = {mount: (element, surface) => { input = {element, surface}; }};
  Object.defineProperty(window.crypto, "randomUUID", {configurable: true, value: () => "request-key"});
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/released-workflows") return response(demoCatalog);
    if (url.startsWith("/api/org/capability/department-1")) return response({
      name: "Carrier Dispatch", capabilities: [],
      projections: [{view: "form", key: "workflow_123456_create_shipment", transition: "create_shipment"}],
      email_records: [{id: "dispatch-1", external_id: "DSP-001", revision: 3, state: "awaiting_carrier"}],
      email_jobs: prepared ? [job] : []
    });
    if (url === "/api/org/workflow-email/department-1/build-1") {
      prepared = true;
      expect(JSON.parse(options.body)).toEqual({record_id: "dispatch-1", recipient: job.recipient,
        subject: job.subject, body: "Please confirm dispatch.", expected_revision: 3,
        idempotency_key: "request-key"});
      return response({job_id: job.id, reference: job.reference});
    }
    if (url.endsWith("/job-1/preview")) return response({
      proposal_id: "approved-1", preview: {
        account: "dispatch@example.com",
        arguments: {to: job.recipient, subject: job.subject, body: "Please confirm dispatch."},
        workflow_email: {attachment: {name: "DSP-001.pdf", base64: "JVBERg=="}}
      }
    });
    return originalFetch(url, options);
  });
  try {
    await reopenWorkflowChat("email_jobs");
    const fields = input.surface.messages[1].updateComponents.components[0].fields;
    expect(fields[0].label).toBe("Which record should this email track?");
    expect(fields[0].type).toBe("text");
    expect(fields[0].options[0].label).toBe("DSP-001 · awaiting carrier");
    expect([...document.querySelectorAll(".workflow-chat-card button")].map(button => button.textContent))
      .toContain("create shipment");
    expect(document.querySelector(".workflow-chat-card").textContent).not.toContain("123456");
    input.element.dispatchEvent(new CustomEvent("austra-workflow-submit", {detail: {
      values: {record_id: "dispatch-1", recipient: job.recipient,
        subject: job.subject, body: "Please confirm dispatch."}, done: () => {}
    }}));
    await new Promise(resolve => setTimeout(resolve, 0));
    const email = document.querySelector("[data-workflow-email]");
    expect(email.textContent).toContain("Workflow email");
    [...email.querySelectorAll("button")].find(item => item.textContent === "Preview approved email").click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(email.querySelector("a[download]").download).toBe("DSP-001.pdf");
    expect(email.textContent).toContain("Send this exact email");
    expect(window.fetch.mock.calls.some(([url]) => url.endsWith("/deliver"))).toBe(false);
  } finally { window.AustraA2UI = originalA2UI; }
});

test("owner notifications are available outside any workflow chat", async () => {
  const title = "Supplier replied";
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/workflow-notifications") return response([{
      id: "alert-1", title, message: "supplier@example.com replied to Purchase request.",
      read: false, created_at: "2026-11-24T10:00:00Z", email: "off"
    }]);
    if (url === "/api/org/workflow-notifications/alert-1/read") return response({read: true});
    return originalFetch(url, options);
  });
  await window.orgTest.refreshConfig();
  const stop = window.startWorkflowAlerts(() => true);
  try {
    const dialog = document.querySelector(".workflow-alert-dialog");
    dialog.showModal = () => dialog.setAttribute("open", "");
    await new Promise(resolve => setTimeout(resolve, 0));
    const button = document.getElementById("workflowAlertsButton");
    expect(button.textContent).toBe("Notifications (1)");
    button.click();
    expect(dialog.textContent).toContain(title);
    [...dialog.querySelectorAll("button")].find(item => item.textContent === "Mark read").click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(button.textContent).toBe("Notifications");
  } finally { stop(); }
});

test("the workflow library shows steps and rules without execution forms", async () => {
  window.history.replaceState({}, "", "/organization/workflows");
  const originalFetch = window.fetch;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/released-workflows") return response(demoCatalog);
    if (url.includes("/api/org/capability/department-1")) return response({
      commands: ["approve_po"], rules: [{decision_key: "command.approve_po",
        required_permission: "workflows.approve", limit: "0", unit: "USD"}]
    });
    return originalFetch(url, options);
  });
  await window.orgTest.refreshConfig();
  await window.orgTest.renderAustraRoute();
  const content = document.getElementById("departmentsContent");
  [...content.querySelectorAll("button")].find(item => item.textContent.includes("Demo Purchase")).click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(content.textContent).toContain("Higher amounts escalate.");
  expect(content.querySelector("form")).toBeNull();
});

async function loadWorkflowProjections() {
  await window.orgTest.refreshConfig();
  const view = await (await window.fetch("/api/org/capability/department-1")).json();
  await window.renderCapabilityViews(document.getElementById("departmentsContent"), view, "department-1");
}

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
    if (url.endsWith("/projections/quality_queue?build_id=build-2")) return response({
      descriptor: projections[1], complete: true, record_count: 1,
      records: [{ id: "record-1", external_id: "BATCH-1", revision: 1,
        state: "received", values: { external_id: "BATCH-1", state: "received", quantity_kg: "2" } }]
    });
    if (url.endsWith("/projections/released_weight?build_id=build-2")) return response({
      descriptor: projections[2], complete: true, record_count: 1, total: "3.5", unit: "kg",
      records: [{ external_id: "BATCH-2", values: { external_id: "BATCH-2", quantity_kg: "3.5" } }]
    });
    if (url === "/api/org/capability-ui/commands") return response({
      event_id: "event-3", blueprint_id: "blueprint-2", build_id: "build-2",
      record: { external_id: "BATCH-3", state: "received", revision: 1 }
    });
    return originalFetch(url, options);
  });
  await loadWorkflowProjections();
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

test("a workflow form failure leaves other projections available and can be retried", async () => {
  window.history.replaceState({}, "", "/organization/capability");
  const originalFetch = window.fetch;
  const projections = [
    { key: "release_form", title: "Release batch", view: "form",
      capability: "quality_batches", fields: ["external_id"], transition: "release" },
    { key: "quality_queue", title: "Quality queue", view: "queue",
      capability: "quality_batches", fields: ["external_id"] }
  ];
  const batch = { id: "batch-1", external_id: "BATCH-1", state: "received", revision: 2,
    values: { external_id: "BATCH-1" } };
  let formRequests = 0;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/capability/department-1") return response({
      build_id: "build-1", projections, capabilities: [{
        key: "quality_batches", fields: [], transitions: [
          { key: "release", from: "received", to: "released", fields: [] }
        ]
      }]
    });
    if (url.endsWith("/projections/release_form?build_id=build-1") && formRequests++ === 0) {
      return { ok: false, status: 503, json: async () => ({ detail: "Records are temporarily unavailable." }) };
    }
    if (url.includes("/projections/")) return response({
      offset: 0, record_count: 1, complete: true, records: [batch]
    });
    return originalFetch(url, options);
  });
  await loadWorkflowProjections();
  const [release, queue] = document.querySelectorAll(".org-capability-panel");
  expect(release.querySelector('[role="alert"]').textContent).toContain("Records are temporarily unavailable.");
  expect(queue.querySelector("td").textContent).toBe("BATCH-1");
  const retry = release.querySelector("button");
  retry.focus();
  retry.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(release.querySelector('[role="alert"]')).toBeNull();
  expect(release.querySelector("select").selectedOptions[0].textContent).toBe("BATCH-1 · r2");
  expect(document.activeElement).toBe(release.querySelector("select"));
});

test("projection pagination keeps the current rows when a request fails and retries the same page", async () => {
  window.history.replaceState({}, "", "/organization/capability");
  const originalFetch = window.fetch;
  const descriptor = { key: "quality_queue", title: "Quality queue", view: "queue",
    fields: ["external_id"] };
  let nextRequests = 0;
  window.fetch = jest.fn((url, options) => {
    if (url === "/api/org/capability/department-1") return response({
      build_id: "build-1", projections: [descriptor], capabilities: []
    });
    const endpoint = new URL(url, "https://austra.test");
    if (endpoint.pathname.endsWith("/projections/quality_queue")) {
      expect(endpoint.searchParams.get("build_id")).toBe("build-1");
      const offset = Number(endpoint.searchParams.get("offset"));
      if (offset && nextRequests++ === 0) {
        return { ok: false, status: 503, json: async () => ({ detail: "Please try again." }) };
      }
      const records = (offset ? ["BATCH-3"] : ["BATCH-1", "BATCH-2"])
        .map(external_id => ({ values: { external_id } }));
      return response({ offset, records, record_count: 3, complete: false });
    }
    return originalFetch(url, options);
  });
  await loadWorkflowProjections();
  const panel = document.querySelector(".org-capability-panel");
  const [previous, next] = panel.querySelectorAll("button");
  expect(previous.disabled).toBe(true);
  expect(panel.querySelector('[role="status"]').textContent).toBe("1–2 of 3 records");
  next.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(panel.querySelector('[role="alert"]').textContent).toContain("Please try again.");
  expect(panel.querySelector("td").textContent).toBe("BATCH-1");
  next.focus();
  next.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(panel.querySelector('[role="status"]').textContent).toBe("3 of 3 records");
  expect(panel.querySelector("td").textContent).toBe("BATCH-3");
  expect(next.disabled).toBe(true);
  expect(document.activeElement).toBe(previous);
  previous.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(panel.querySelector("td").textContent).toBe("BATCH-1");
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
  await window.orgTest.renderAustraRoute();
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
