/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");
let channels;

function response(data, status = 200) {
  return { ok: status < 400, status, json: async () => data };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  channels = [];
  window.BroadcastChannel = class {
    constructor() { channels.push(this); }
    postMessage = jest.fn();
  };
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.requestAnimationFrame = callback => callback();
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "platform.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-detail.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-actions.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(
    /\nstart\(\);\s*$/,
    "\nwindow.authTest = { api, refreshConfig, showConnections, showSignedOut, showModal, navigateAustra };"
  ));
  window.fetch = jest.fn(async url => url === "/api/config"
    ? response({
      admin_ready: true,
      authenticated: true,
      session_seconds_remaining: 3600,
      connectors: { anthropic: false }
    })
    : response({ authenticated: false }));
});

afterEach(() => {
  window.authTest.showSignedOut(false);
  delete window.authTest;
  delete window.BroadcastChannel;
  delete window.AustraA2UI;
  jest.useRealTimers();
});

test("sign out shows sign in immediately and removes private content", async () => {
  await window.authTest.refreshConfig();
  document.getElementById("chatList").textContent = "Private chat";
  document.getElementById("messages").textContent = "Private answer";
  document.getElementById("logoutButton").click();
  await new Promise(resolve => setTimeout(resolve, 0));

  expect(window.fetch).toHaveBeenCalledWith("/api/logout", expect.objectContaining({ method: "POST" }));
  expect(document.getElementById("appShell").classList.contains("signed-out")).toBe(true);
  expect(document.getElementById("messages").textContent).toContain("Sign in");
  expect(document.getElementById("messages").textContent).not.toContain("Private answer");
  expect(document.getElementById("chatList").textContent).toBe("");
  expect(channels[0].postMessage).toHaveBeenCalledWith("signed-out");
});

test("temporary employee credentials can reach the password change screen before department lookup", async () => {
  window.fetch = jest.fn(async url => {
    if (url !== "/api/config") throw new Error("Unexpected protected request " + url);
    return response({ authenticated: true, role: "employee", legacy_workspace: false,
      must_change_password: true, connectors: { anthropic: true }, session_seconds_remaining: 120 });
  });
  await window.authTest.refreshConfig();
  expect(document.getElementById("askDepartment").hidden).toBe(true);
  expect(window.fetch).toHaveBeenCalledTimes(1);
});

test("organization admin can open the department ownership view", async () => {
  window.fetch = jest.fn(async url => {
    if (url === "/api/config") return response({
      admin_ready: true, authenticated: true, saas_mode: true, role: "org_admin",
      organization_name: "Polysilicon Manufacturing Plant",
      session_seconds_remaining: 3600, connectors: { anthropic: false }
    });
    if (url === "/api/org/departments") return response([
      { id: "department-id", slug: "inventory", name: "Inventory", object_count: 52 }
    ]);
    if (url === "/api/org/users") return response([
      { id: "admin-id", username: "admin", role: "org_admin", status: "active" }
    ]);
    return response({});
  });
  await window.authTest.refreshConfig();
  const button = document.getElementById("departmentsButton");
  expect(button.hidden).toBe(false);
  button.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(document.getElementById("departmentsContent").textContent).toContain("Inventory");
  expect(document.getElementById("departmentsContent").textContent).toContain("52");
  expect(window.location.pathname).toBe("/organization/departments");
});

test("organization info has its own route and a summary for legacy organizations", async () => {
  window.fetch = jest.fn(async url => {
    if (url !== "/api/config") throw new Error("Unexpected request " + url);
    return response({ authenticated: true, saas_mode: true, legacy_workspace: true, role: "org_admin",
      organization_name: "Northstar", connectors: { anthropic: true }, session_seconds_remaining: 3600 });
  });
  await window.authTest.refreshConfig();
  await window.authTest.navigateAustra("/organization/info");

  expect(window.location.pathname).toBe("/organization/info");
  expect(document.getElementById("accountTitle").textContent).toBe("Organization info");
  expect(document.querySelector("#accountContent dd").textContent).toBe("Northstar");
  expect(document.getElementById("departmentsPage").hidden).toBe(true);
  expect(document.getElementById("orgInfoButton").getAttribute("aria-current")).toBe("page");
});

test("employees are not sent to organization administration or model credential APIs", async () => {
  window.fetch = jest.fn(async url => {
    if (url === "/api/config") return response({
      authenticated: true, saas_mode: true, legacy_workspace: false, role: "employee",
      connectors: { anthropic: true }, session_seconds_remaining: 3600
    });
    if (url === "/api/org/ask/departments") return response([]);
    throw new Error("Unexpected protected request " + url);
  });
  await window.authTest.refreshConfig();
  for (const page of ["/organization/people", "/organization/info", "/connections"]) {
    await window.authTest.navigateAustra(page);
    expect(window.location.pathname).toBe("/");
    expect(document.getElementById("chatArea").hidden).toBe(false);
  }
  expect(document.getElementById("organizationNavigation").hidden).toBe(true);
  expect(document.getElementById("connectionsButton").hidden).toBe(true);
  expect(window.fetch).toHaveBeenCalledTimes(2);
});

test("workspace ignores an older response after returning to the same section", async () => {
  await window.authTest.refreshConfig();
  const workspace = label => ({ surface: { label }, items: [], total: 1, page_size: 20 });
  let resolveOldRecords;
  let recordsCalls = 0;
  window.AustraA2UI = { mount: (target, surface) => { target.textContent = surface.label; } };
  window.fetch = jest.fn(async url => {
    if (url.includes("section=files")) return response({ items: [], total: 0, page_size: 20 });
    if (url.includes("section=records")) {
      recordsCalls += 1;
      if (recordsCalls === 1) return new Promise(resolve => { resolveOldRecords = resolve; });
      return response(workspace("Current records"));
    }
    throw new Error("Unexpected request " + url);
  });
  await window.authTest.navigateAustra("/workspace");
  expect(document.getElementById("workspacePagination").hidden).toBe(true);
  const files = document.querySelector('[data-section="files"]');
  files.focus();
  files.click();
  expect(document.activeElement).toBe(files);
  document.querySelector('[data-section="records"]').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  resolveOldRecords(response(workspace("Stale records")));
  await new Promise(resolve => setTimeout(resolve, 0));

  expect(document.getElementById("workspaceContent").textContent).toBe("Current records");
  expect(document.getElementById("workspacePagination").hidden).toBe(false);
});

test("saving Gmail OAuth details directs the user to consent again", async () => {
  window.fetch = jest.fn(async url => {
    if (url === "/api/config") return response({
      admin_ready: true, authenticated: true, legacy_workspace: true,
      session_seconds_remaining: 3600,
      connectors: { gmail: true },
      guides: { gmail: { title: "Gmail", steps: "Connect Gmail", url: "https://example.test" } },
      fields: { gmail: ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"] },
      missing: { gmail: [] },
      gmail_redirect_uri: "http://localhost:8000/api/connections/gmail/callback"
    });
    if (url === "/api/credentials/gmail") return response({ configured: true });
    throw new Error("Unexpected request " + url);
  });

  await window.authTest.refreshConfig();
  window.authTest.showConnections("gmail");
  document.querySelector('[aria-label="GMAIL_CLIENT_SECRET"]').value = "replacement-secret";
  await document.querySelector("#connectionDetail form").onsubmit({ preventDefault() {} });

  expect(document.getElementById("connectionFeedback").textContent)
    .toBe("OAuth client saved. Choose Connect with Google.");
  expect(window.fetch).toHaveBeenCalledTimes(3);
});

test("expired session on an API response returns to sign in without reload", async () => {
  await window.authTest.refreshConfig();
  window.fetch = jest.fn(async () => response({ detail: "Administrator sign in required" }, 401));

  await expect(window.authTest.api("/api/chats")).rejects.toThrow("Sign in again");
  expect(document.getElementById("appShell").classList.contains("signed-out")).toBe(true);
  const password = document.querySelector('input[autocomplete="current-password"]');
  password.value = "typing a new passphrase";
  await expect(window.authTest.api("/api/chats")).rejects.toThrow("Sign in again");
  expect(password.value).toBe("typing a new passphrase");
});

test("the session expiry timer switches to sign in without network polling", async () => {
  jest.useFakeTimers();
  window.fetch = jest.fn(async () => response({
    admin_ready: true,
    authenticated: true,
    session_seconds_remaining: 1,
    connectors: { anthropic: false }
  }));
  await window.authTest.refreshConfig();
  const calls = window.fetch.mock.calls.length;

  jest.advanceTimersByTime(1000);

  expect(document.getElementById("appShell").classList.contains("signed-out")).toBe(true);
  expect(window.fetch).toHaveBeenCalledTimes(calls);
});

test("sign out in another tab switches this tab to sign in", async () => {
  await window.authTest.refreshConfig();
  channels[0].onmessage({ data: "signed-out" });

  expect(document.getElementById("appShell").classList.contains("signed-out")).toBe(true);
  expect(channels[0].postMessage).not.toHaveBeenCalled();
});

test("a response started before sign out cannot restore private data", async () => {
  await window.authTest.refreshConfig();
  let completeRequest;
  window.fetch = jest.fn(() => new Promise(resolve => { completeRequest = resolve; }));
  const pending = window.authTest.api("/api/chats");

  window.authTest.showSignedOut(false);
  completeRequest(response([{ title: "Private chat" }]));

  await expect(pending).rejects.toThrow("Sign in again");
  expect(document.getElementById("chatList").textContent).toBe("");
  expect(document.getElementById("messages").textContent).toContain("Sign in");
});

test("a rejected sign in shows its error on the sign in screen", async () => {
  await window.authTest.refreshConfig();
  window.authTest.showSignedOut(false);
  window.fetch = jest.fn(async () => response({ detail: "Incorrect passphrase" }, 401));
  document.querySelector('input[autocomplete="current-password"]').value = "wrong passphrase";

  [...document.querySelectorAll("button")].find(button => button.textContent === "Sign in").click();
  await new Promise(resolve => setTimeout(resolve, 0));

  expect(document.querySelector(".auth-feedback").textContent).toBe("Incorrect passphrase");
  expect(document.getElementById("appShell").classList.contains("signed-out")).toBe(true);
});

test("session expiry closes a modal and leaves the sign in usable", async () => {
  await window.authTest.refreshConfig();
  const dialog = window.authTest.showModal({ title: "Delete chat?", message: "Private chat" });
  expect(document.getElementById("appShell").hasAttribute("inert")).toBe(true);

  window.authTest.showSignedOut(false);

  await expect(dialog).resolves.toBe(false);
  expect(document.getElementById("appShell").hasAttribute("inert")).toBe(false);
  expect(document.querySelector('input[autocomplete="current-password"]')).not.toBeNull();
});
