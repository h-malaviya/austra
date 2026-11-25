/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder, TextEncoder } = require("node:util");

const staticRoot = path.join(__dirname, "../src/austra/static");
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const streamResponse = data => {
  const bytes = Buffer.from(`event: done\ndata: ${JSON.stringify(data)}\n\n`);
  let sent = false;
  return { ...response(data), body: { getReader: () => ({ read: async () => {
    if (sent) return { done: true };
    sent = true;
    return { value: bytes, done: false };
  } }) } };
};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
let config;
let tour;
let knowledgeChats;
let restoreReplaceSync;

beforeAll(() => {
  if (!document.adoptedStyleSheets) document.adoptedStyleSheets = [];
  const original = CSSStyleSheet.prototype.replaceSync;
  const styles = [];
  CSSStyleSheet.prototype.replaceSync = function (css) {
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
    styles.push(style);
  };
  restoreReplaceSync = () => {
    styles.forEach(style => style.remove());
    if (original) CSSStyleSheet.prototype.replaceSync = original;
    else delete CSSStyleSheet.prototype.replaceSync;
  };
});

afterAll(() => restoreReplaceSync?.());

beforeEach(() => {
  window.history.replaceState({}, "", "/organization/onboarding");
  document.body.innerHTML = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8")
    .match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.requestAnimationFrame = callback => callback();
  window.TextDecoder = TextDecoder;
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "platform.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-detail.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "organization-admin-actions.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8").replace(
    /\nstart\(\);\s*$/, "\nwindow.tourTest = { refreshConfig, renderTour, renderWelcome, renderOnboardingChat, showWorkspaceOauthResult, loadChats, addPendingFiles, sendMessage, showChat, showSignedOut };"
  ));
  config = {
    admin_ready: true, authenticated: true, saas_mode: true, legacy_workspace: false,
    role: "org_admin", organization_name: "Acme", session_seconds_remaining: 3600,
    connectors: { anthropic: false }
  };
  tour = {
    completed: false, organization_name: "Acme", username: "admin", full_name: "Ava",
    email: "ava@example.com", contact_number: null, about_admin: null,
    about_organization: null, website_url: null, social_url: null, revision: 0,
    messages: [], website_status: null, discovery_state: {}
  };
  knowledgeChats = [];
  window.fetch = jest.fn(async (url, options = {}) => {
    if (url === "/api/config") return response(config);
    if (url === "/api/org/tour") return response(tour);
    if (url === "/api/credentials/anthropic") {
      if (JSON.parse(options.body).values.ANTHROPIC_API_KEY !== "valid-key") {
        return response({ detail: "Key rejected" }, 400);
      }
      config.connectors.anthropic = true;
      return response({ configured: true });
    }
    if (url === "/api/org/tour/start") {
      tour.messages.push({ role: "assistant", body: "Hello! Who are you at Acme?" });
      tour.revision += 1;
      return response(tour);
    }
    if (url === "/api/org/tour/message/stream") {
      const { body, revision } = JSON.parse(options.body);
      if (revision !== tour.revision) return response({ detail: "Stale tour" }, 409);
      tour.messages.push({ role: "user", body });
      if (body.includes("Ava")) {
        tour.about_admin = body;
        tour.messages.push({ role: "assistant", body: "What does Acme make?" });
      } else if (body.includes("bicycles")) {
        tour.about_organization = body;
        tour.messages.push({ role: "assistant", body: "Can I read your website?" });
      } else if (body.includes("https://")) {
        tour.website_url = body;
        tour.website_status = "Read 3 public pages from https://acme.example.";
        tour.messages.push({ role: "assistant", body: "I read 3 public pages. Does Acme also have a LinkedIn page?" });
      } else {
        tour.completed = true;
        tour.messages.push({ role: "assistant", body: "Company introduction complete." });
      }
      tour.revision += 1;
      return streamResponse(tour);
    }
    if (url === "/api/org/discovery/message/stream") {
      const { body, revision, attachments = [] } = JSON.parse(options.body);
      if (revision !== tour.revision) return response({ detail: "Stale conversation" }, 409);
      tour.messages.push({ role: "user", body: body + attachments.map(id => `\n📎 ${id.slice(7)}.txt`).join("") });
      tour.messages.push({ role: "assistant", body: "Who follows up with the client?" });
      tour.discovery_state.chat_deleted = false;
      tour.revision += 1;
      return streamResponse(tour);
    }
    if (url === "/api/org/discovery/chat" && options.method === "DELETE") {
      tour.messages = [];
      tour.discovery_state = { chat_deleted: true };
      tour.revision += 1;
      return response(tour);
    }
    if (url === "/api/org/chats") {
      return response(knowledgeChats.map(chat => ({ id: chat.id, title: chat.title })));
    }
    if (url === "/api/org/onboarding-sources/files") {
      const file = options.body.get("upload");
      return response({ source_id: `source-${file.name.replace(/\.txt$/, "")}`,
        revision_id: `revision-${file.name.replace(/\.txt$/, "")}` }, 201);
    }
    if (url.startsWith("/api/org/onboarding-sources/revisions/") && url.endsWith("/process")) {
      return response({ state: "complete" });
    }
    if (url.startsWith("/api/org/chats/")) {
      const chat = knowledgeChats.find(item => item.id === url.split("/").at(-1));
      if (options.method === "DELETE") {
        knowledgeChats = knowledgeChats.filter(item => item !== chat);
        return response({ deleted: true });
      }
      return response({ chat, drafts: [] });
    }
    if (url === "/api/org/ask/stream") {
      const { question, chat_id } = JSON.parse(options.body);
      let chat = knowledgeChats.find(item => item.id === chat_id);
      if (!chat) {
        chat = { id: "knowledge-1", title: question, messages: [] };
        knowledgeChats.push(chat);
      }
      chat.messages.push({ id: `user-${chat.messages.length}`, role: "user", body: question });
      chat.messages.push({ id: `assistant-${chat.messages.length}`, role: "assistant",
        body: "No approved information covers that yet." });
      return streamResponse({ answer: { answer: "No approved information covers that yet." }, chat });
    }
    return response({});
  });
});

afterEach(() => {
  window.tourTest.showSignedOut(false);
  delete window.tourTest;
  delete window.AustraA2UI;
  jest.restoreAllMocks();
});

function workspaceConsent(messageId, workflowId = null) {
  const id = "workspace_consent_" + messageId;
  const catalogId = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json";
  return {
    id, kind: "workspace_consent", message_id: messageId,
    app: "Google Sheets", workflow_id: workflowId,
    messages: [
      { version: "v0.9.1", createSurface: { surfaceId: id, catalogId } },
      { version: "v0.9.1", updateComponents: { surfaceId: id, components: [
        { id: "label", component: "Text", text: "Connect Google Workspace", variant: "body" },
        { id: "connect", component: "Button", child: "label", variant: "default",
          action: { event: { name: "connect_google_workspace", context: {} } } },
        { id: "root", component: "Row", children: ["connect"] }
      ] } }
    ]
  };
}

function captureA2UI() {
  const unmount = jest.fn();
  window.AustraA2UI = { mount: jest.fn(() => unmount) };
  return { mount: window.AustraA2UI.mount, unmount };
}

test("Google Workspace consent opens separately and restores the selected workflow", async () => {
  tour.completed = true;
  tour.workflows = [
    { id: "test-draft", name: "Stock review", state: "draft" },
    { id: "other-draft", name: "Another workflow", state: "draft" }
  ];
  const { mount, unmount } = captureA2UI();
  const consent = workspaceConsent("answer", "test-draft");
  tour.messages = [
    { role: "assistant", body: "Which sheet contains the counts?" },
    { id: "answer", role: "assistant", body: "I need access to the stock sheet.", workspace_consent: consent }
  ];
  const destination = "https://accounts.google.com/o/oauth2/v2/auth?state=fixture";
  let apps = [{ name: "Google Drive", health: "healthy" }];
  const fetch = window.fetch;
  window.fetch = jest.fn(async (url, options) => {
    if (url === "/api/org/connections/google-workspace/start") {
      expect(JSON.parse(options.body)).toEqual({ workflow_id: "test-draft" });
      return response({ authorization_url: destination });
    }
    if (url === "/api/org/connections/google-workspace/status") return response({ apps });
    return fetch(url, options);
  });
  const popup = { location: { replace: jest.fn() }, close: jest.fn(), opener: window };
  jest.spyOn(window, "open").mockReturnValueOnce(null).mockReturnValueOnce(popup);
  await window.tourTest.refreshConfig();
  window.tourTest.renderOnboardingChat("test-draft");
  document.getElementById("messageInput").value = "My unsent note";
  expect(mount).toHaveBeenCalledTimes(1);
  const [holder, description, dispatch] = mount.mock.calls[0];
  expect(description).toEqual(consent);
  expect(holder.closest(".message-row.assistant > .bubble").textContent).toContain("access to the stock sheet");
  document.getElementById("discoveryWorkflow").value = "other-draft";
  const feedback = holder.querySelector('[role="status"]');
  await dispatch({ name: "connect_google_workspace" });
  expect(feedback.textContent).toMatch(/Allow Austra/);
  await dispatch({ name: "connect_google_workspace" });
  expect(popup.location.replace).toHaveBeenCalledWith(destination);
  expect(popup.opener).toBeNull();
  expect(document.getElementById("messageInput").value).toBe("My unsent note");
  expect(document.getElementById("messages").textContent).toContain("Which sheet contains the counts?");
  await dispatch({ name: "check_google_workspace" });
  expect(feedback.textContent).toContain("Access to Google Sheets is still pending");
  apps = [{ name: "Google Sheets", health: "healthy" }];
  await dispatch({ name: "check_google_workspace" });
  expect(feedback.textContent).toContain("Google consent is saved for Google Sheets");
  expect(unmount).toHaveBeenCalledTimes(1);

  window.history.replaceState({}, "", "/?workspace_oauth=denied&workflow=test-draft");
  window.tourTest.showWorkspaceOauthResult();
  expect(document.getElementById("discoveryWorkflow").value).toBe("test-draft");
  expect(document.getElementById("status").textContent).toMatch(/declined/);
  expect(window.location.search).toBe("");
});

test("Google consent is absent on welcome and in regular chat, and mounts only in its assistant reply", async () => {
  config.legacy_workspace = true;
  window.eval(fs.readFileSync(path.join(staticRoot, "a2ui.bundle.js"), "utf8"));
  const mount = jest.spyOn(window.AustraA2UI, "mount");
  const chat = { id: "stock", title: "Stock review", messages: [
    { id: "question", role: "user", body: "Draft a stock review workflow" },
    { id: "answer", role: "assistant", body: "I saved the draft." }
  ] };
  let surfaces = [];
  const fetch = window.fetch;
  window.fetch = jest.fn(async (url, options) => {
    if (url === "/api/chats") return response([]);
    if (url === "/api/chats/stock") return response({ chat });
    if (url === "/api/ui/chats/stock") return response({ surfaces });
    return fetch(url, options);
  });
  await window.tourTest.refreshConfig();
  window.tourTest.renderWelcome();
  expect(document.querySelector("#messages").textContent).not.toContain("Connect Google Workspace");
  await window.tourTest.showChat("stock");
  await settle();
  expect(mount).not.toHaveBeenCalled();
  expect(document.querySelector("#messages").textContent).not.toContain("Connect Google Workspace");

  const consent = workspaceConsent("blocked");
  chat.messages.push({ id: "blocked", role: "assistant", body: "I need access to the stock sheet." });
  surfaces = [workspaceConsent("missing"), workspaceConsent("question"), consent];
  await window.tourTest.showChat("stock");
  await settle();
  expect(mount).toHaveBeenCalledTimes(1);
  const [holder, description] = mount.mock.calls[0];
  expect(description).toEqual(consent);
  expect(holder.closest(".bubble").dataset.messageId).toBe("blocked");
  const button = holder.querySelector("a2ui-basic-button button");
  expect(button.textContent).toContain("Connect Google Workspace");
  jest.spyOn(window, "open").mockReturnValue(null);
  button.click();
  await settle();
  expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
});

test("company introduction unlocks pages before workflow discovery", async () => {
  await window.tourTest.refreshConfig();
  window.tourTest.renderTour();
  expect(document.getElementById("appShell").classList.contains("tour-mode")).toBe(true);
  expect(document.getElementById("orgInfoButton").hidden).toBe(true);
  expect(document.getElementById("messages").textContent).toContain("Anthropic API key");

  const key = document.querySelector('input[aria-label="Anthropic API key"]');
  key.value = "wrong";
  [...document.querySelectorAll("button")].find(button => button.textContent.includes("Check key")).click();
  await settle();
  expect(document.querySelector(".auth-feedback").textContent).toBe("Key rejected");
  expect(tour.completed).toBe(false);

  key.value = "valid-key";
  [...document.querySelectorAll("button")].find(button => button.textContent.includes("Check key")).click();
  await settle();
  expect(document.getElementById("messages").textContent).toContain("Who are you at Acme?");
  expect(document.getElementById("connection").textContent).toBe("Austra model connected");
  await window.tourTest.refreshConfig();
  expect(document.getElementById("connection").textContent).toBe("Austra model connected");
  const input = document.getElementById("messageInput");
  const reply = async body => { input.value = body; await window.tourTest.sendMessage(); };
  await reply("I’m Ava, the owner.");
  expect(tour.about_admin).toBe("I’m Ava, the owner.");
  expect(document.getElementById("messages").textContent).toContain("What does Acme make?");
  await reply("We build bicycles.");
  expect(tour.about_organization).toBe("We build bicycles.");
  await reply("https://acme.example");
  expect(document.getElementById("messages").textContent).toContain("I read 3 public pages");
  expect(document.querySelector(".tour-source-status")).toBeNull();
  expect(document.getElementById("orgInfoButton").hidden).toBe(true);
  await reply("Yes, that’s right.");
  expect(tour.completed).toBe(true);
  expect(document.getElementById("appShell").classList.contains("tour-mode")).toBe(false);
  expect(document.getElementById("orgInfoButton").hidden).toBe(false);
  expect(document.getElementById("departmentsButton").hidden).toBe(false);
  expect(document.getElementById("newChat").textContent).toContain("New chat");
  expect(document.getElementById("chatList").textContent).toContain("Acme onboarding");
  expect(document.getElementById("chatTitle").textContent).toBe("Acme onboarding");
  await reply("We need help following up with new clients.");
  expect(document.getElementById("messages").textContent).toContain("Who follows up");
  expect(window.fetch).toHaveBeenCalledWith("/api/org/discovery/message/stream", expect.anything());
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/ask/stream", expect.anything());
  document.getElementById("newChat").click();
  expect(document.getElementById("chatTitle").textContent).toBe("New chat");
  expect(document.getElementById("messages").textContent).not.toContain("Who follows up");
  await reply("What information is approved?");
  expect(window.fetch).toHaveBeenCalledWith("/api/org/ask/stream", expect.anything());
  [...document.querySelectorAll("#chatList .chat-link")]
    .find(button => button.title === "Acme onboarding").click();
  expect(document.getElementById("messages").textContent).toContain("Who follows up");
});

test("profile stays available during the tour", async () => {
  await window.tourTest.refreshConfig();
  window.tourTest.renderTour();
  document.getElementById("profileButton").click();
  expect(document.getElementById("accountContent").textContent).toContain("Ava");
  expect(document.getElementById("accountContent").textContent).toContain("Acme");
  expect(document.getElementById("departmentsButton").hidden).toBe(true);
});

test.each([
  ["My profile", "profileButton", "departmentsButton", "departmentsPage"],
  ["Organization info", "orgInfoButton", "connectionsButton", "connectionsPage"]
])("leaving %s displays only the destination page", async (heading, accountButton, nextButton, nextPage) => {
  tour.completed = true;
  config.guides = { anthropic: { title: "Austra model" } };
  config.fields = { anthropic: [] };
  const fetch = window.fetch;
  window.fetch = jest.fn((url, options) => url === "/api/org/departments"
    ? Promise.resolve(response([])) : fetch(url, options));
  await window.tourTest.refreshConfig();

  document.getElementById(accountButton).click();
  expect(document.querySelector("#accountPage h1").textContent).toBe(heading);
  expect(document.getElementById("accountPage").hidden).toBe(false);

  document.getElementById(nextButton).click();
  await settle();

  const visiblePages = [...document.querySelectorAll("#appShell > main:not([hidden])")];
  expect(visiblePages.map(page => page.id)).toEqual([nextPage]);
});

test("discovery shows actual activity and hides unfinished model output", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderOnboardingChat();
  const originalFetch = window.fetch;
  const snapshots = [];
  window.fetch = jest.fn(async (url, options) => {
    if (url !== "/api/org/discovery/message/stream") return originalFetch(url, options);
    const body = JSON.parse(options.body).body;
    tour.messages.push({ role: "user", body }, { role: "assistant", body: "Final answer" });
    tour.revision += 1;
    const frames = [
      'event: stage\ndata: {"stage":"Thinking"}\n\n',
      `event: delta\ndata: ${JSON.stringify({ text: '{"reply":"First attempt"}' })}\n\n`,
      'event: reset\ndata: {}\n\n',
      'event: stage\ndata: {"stage":"Reviewing source information"}\n\n',
      `event: done\ndata: ${JSON.stringify(tour)}\n\n`
    ];
    let index = 0;
    return { ok: true, status: 200, body: { getReader: () => ({ read: async () => {
      snapshots.push(document.getElementById("messages").textContent);
      return index < frames.length
        ? { value: Buffer.from(frames[index++]), done: false } : { done: true };
    } }) } };
  });

  document.getElementById("messageInput").value = "Who decides?";
  await window.tourTest.sendMessage();

  expect(snapshots[1]).toContain("Thinking");
  expect(snapshots[2]).not.toContain("First attempt");
  expect(snapshots[3]).not.toContain("First attempt");
  expect(snapshots[4]).toContain("Reviewing source information");
  expect(snapshots[4]).not.toContain("Final answer");
  expect(document.getElementById("messages").textContent).not.toContain("First attempt");
  expect(document.querySelectorAll(".streaming-text")).toHaveLength(0);
  expect(document.getElementById("messages").textContent).toContain("Final answer");
});

test("send arrow stays visible and disabled while a question is processing", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderWelcome();
  const originalFetch = window.fetch;
  let releaseRequest;
  const gate = new Promise(resolve => { releaseRequest = resolve; });
  window.fetch = jest.fn(async (url, options) => {
    if (url === "/api/org/ask/stream") await gate;
    return originalFetch(url, options);
  });

  document.getElementById("messageInput").value = "What do we know?";
  const sending = window.tourTest.sendMessage();
  const sendButton = document.getElementById("sendButton");
  expect(sendButton.disabled).toBe(true);
  expect(sendButton.textContent).toBe("➤");

  releaseRequest();
  await sending;
  expect(sendButton.disabled).toBe(false);
  expect(sendButton.textContent).toBe("➤");
});

test("older cached page missing department selector still starts without a crash", async () => {
  document.getElementById("askDepartment").remove();
  await expect(window.tourTest.refreshConfig()).resolves.toBeUndefined();
  window.tourTest.renderWelcome();
  expect(document.getElementById("chatTitle").textContent).toBe("New chat");
});

test("tour keeps structured output hidden while reporting a website visit", async () => {
  config.connectors.anthropic = true;
  tour.messages = [{ role: "assistant", body: "What is your website?" }];
  tour.revision = 1;
  await window.tourTest.refreshConfig();
  window.tourTest.renderTour();
  const originalFetch = window.fetch;
  const snapshots = [];
  const sendStates = [];
  window.fetch = jest.fn(async (url, options) => {
    if (url !== "/api/org/tour/message/stream") return originalFetch(url, options);
    tour.messages.push(
      { role: "user", body: "https://acme.example" },
      { role: "assistant", body: "I read the public website." }
    );
    tour.revision += 1;
    const frames = [
      'event: stage\ndata: {"stage":"Checking company details"}\n\n',
      `event: delta\ndata: ${JSON.stringify({ text: '{"reply":"Unfinished"}' })}\n\n`,
      'event: stage\ndata: {"stage":"Visiting website"}\n\n',
      `event: done\ndata: ${JSON.stringify(tour)}\n\n`
    ];
    let index = 0;
    return { ok: true, status: 200, body: { getReader: () => ({ read: async () => {
      snapshots.push(document.getElementById("messages").textContent);
      sendStates.push({
        disabled: document.getElementById("sendButton").disabled,
        label: document.getElementById("sendButton").textContent,
        inputDisabled: document.getElementById("messageInput").disabled,
        attachDisabled: document.getElementById("attachButton").disabled,
        messagesBusy: document.getElementById("messages").getAttribute("aria-busy")
      });
      return index < frames.length
        ? { value: Buffer.from(frames[index++]), done: false } : { done: true };
    } }) } };
  });

  document.getElementById("messageInput").value = "https://acme.example";
  await window.tourTest.sendMessage();

  expect(snapshots[0]).toContain("Checking company details");
  expect(snapshots[2]).not.toContain("Unfinished");
  expect(snapshots[3]).toContain("Visiting website");
  expect(snapshots[3]).not.toContain("I read the public website.");
  expect(document.getElementById("messages").textContent).toContain("I read the public website.");
  expect(document.getElementById("messages").textContent).not.toContain("Unfinished");
  expect(sendStates.every(state => state.disabled && state.label === "➤"
    && state.inputDisabled && state.attachDisabled && state.messagesBusy === "true")).toBe(true);
  expect(document.getElementById("messageInput").disabled).toBe(false);
  expect(document.getElementById("attachButton").disabled).toBe(false);
  expect(document.getElementById("messages").hasAttribute("aria-busy")).toBe(false);
});

test("onboarding chat has a delete menu and a new conversation can start", async () => {
  tour.completed = true;
  tour.about_organization = "Acme builds bikes";
  tour.messages = [
    { role: "user", body: "Old process" },
    { role: "assistant", body: "Who approves it?" }
  ];
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  await window.tourTest.loadChats();
  window.tourTest.renderOnboardingChat();

  const options = document.querySelector('#chatList [aria-label="Options for Acme onboarding"]');
  expect(options).not.toBeNull();
  options.click();
  expect(options.getAttribute("aria-expanded")).toBe("true");
  document.querySelector('[aria-label="Delete chat Acme onboarding"]').click();
  await settle();
  expect(document.querySelector(".confirmation-dialog").textContent).toContain("confirmed company details");
  document.querySelector(".confirmation-dialog .primary").click();
  await settle();
  await settle();

  expect(window.fetch).toHaveBeenCalledWith("/api/org/discovery/chat", expect.objectContaining({ method: "DELETE" }));
  expect(document.getElementById("chatList").textContent).not.toContain("Acme onboarding");
  expect(document.getElementById("messages").textContent).not.toContain("Old process");
  expect(tour.about_organization).toBe("Acme builds bikes");
  [...document.querySelectorAll("#messages button")]
    .find(button => button.textContent === "Plan a process").click();
  document.getElementById("messageInput").value = "Start again";
  await window.tourTest.sendMessage();
  expect(document.getElementById("chatList").textContent).toContain("Acme onboarding");
  expect(document.getElementById("messages").textContent).toContain("Who follows up");
});

test("knowledge chats stay visible and continue by chat id", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderWelcome();
  expect(document.getElementById("chatList").hidden).toBe(false);
  await window.tourTest.loadChats();
  expect(document.getElementById("chatList").textContent).toContain("Acme onboarding");
  const input = document.getElementById("messageInput");
  input.value = "What is approved?";
  await window.tourTest.sendMessage();
  expect(knowledgeChats[0].messages).toHaveLength(2);
  await window.tourTest.showChat("knowledge-1");
  input.value = "And what else?";
  await window.tourTest.sendMessage();
  expect(knowledgeChats[0].messages).toHaveLength(4);
  const asks = window.fetch.mock.calls.filter(([url]) => url === "/api/org/ask/stream");
  expect(JSON.parse(asks[0][1].body).chat_id).toBeNull();
  expect(JSON.parse(asks[1][1].body).chat_id).toBe("knowledge-1");
});

test("attached files are unique per message, removable, and sent together", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderOnboardingChat();
  expect(document.getElementById("attachButton").hidden).toBe(false);
  const file = (name, content) => {
    const item = new File([content], name, { type: "text/plain", lastModified: 1 });
    item.arrayBuffer = async () => new TextEncoder().encode(content).buffer;
    return item;
  };
  await window.tourTest.addPendingFiles([
    file("first.txt", "First"), file("first.txt", "First"),
    file("copy.txt", "First"), file("second.txt", "Second")
  ]);
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(2);
  expect(document.getElementById("status").textContent).toContain("2 duplicate files");
  document.querySelector('[aria-label="Remove first.txt"]').click();
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(1);
  await window.tourTest.addPendingFiles([file("first.txt", "First")]);
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(2);

  await window.tourTest.sendMessage();

  const uploads = window.fetch.mock.calls.filter(([url]) => url === "/api/org/onboarding-sources/files");
  expect(uploads).toHaveLength(2);
  const sent = window.fetch.mock.calls.find(([url]) => url === "/api/org/discovery/message/stream");
  expect(JSON.parse(sent[1].body).attachments).toEqual(["source-second", "source-first"]);
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(0);
  expect(document.getElementById("messages").textContent).toContain("📎 first.txt");
});

test("image files can be selected, duplicates skipped, and removed", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderOnboardingChat();
  expect(document.getElementById("fileInput").accept).toContain(".png");
  expect(document.getElementById("fileInput").accept).toContain(".webp");
  const image = (name, content) => {
    const item = new File([content], name, { type: "image/png", lastModified: 1 });
    item.arrayBuffer = async () => new TextEncoder().encode(content).buffer;
    return item;
  };
  await window.tourTest.addPendingFiles([
    image("photo.png", "pixels"), image("copy.jpg", "pixels"),
    image("other.webp", "different")
  ]);
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(2);
  expect(document.getElementById("status").textContent).toContain("duplicate file skipped");
  document.querySelector('[aria-label="Remove photo.png"]').click();
  expect(document.querySelectorAll(".attachment-chip")).toHaveLength(1);
});

test("the highlighted chat follows the open chat and clears on a new chat", async () => {
  tour.completed = true;
  knowledgeChats = [
    { id: "chat-one", title: "First chat", messages: [] },
    { id: "chat-two", title: "Second chat", messages: [] }
  ];
  await window.tourTest.refreshConfig();
  await window.tourTest.loadChats();
  await window.tourTest.showChat("chat-one");
  expect(document.querySelector('[data-chat-id="chat-one"]').getAttribute("aria-current")).toBe("page");
  expect(document.querySelector('[data-chat-id="chat-one"]').closest(".chat-list-item").classList.contains("active")).toBe(true);
  expect(document.querySelector('[data-chat-id="chat-two"]').classList.contains("active")).toBe(false);
  expect(document.querySelector('[data-chat-id="chat-two"]').closest(".chat-list-item").classList.contains("active")).toBe(false);
  document.querySelector('[data-chat-mode="discovery"]').click();
  expect(document.querySelector('[data-chat-mode="discovery"]').classList.contains("active")).toBe(true);
  expect(document.querySelector('[data-chat-mode="discovery"]').closest(".chat-list-item").classList.contains("active")).toBe(true);
  expect(document.querySelector('[data-chat-id="chat-one"]').classList.contains("active")).toBe(false);
  await window.tourTest.showChat("chat-two");
  expect(document.querySelector('[data-chat-id="chat-two"]').getAttribute("aria-current")).toBe("page");
  window.tourTest.renderWelcome();
  expect(document.querySelectorAll(".chat-link.active")).toHaveLength(0);
  expect(document.querySelectorAll(".chat-list-item.active")).toHaveLength(0);
});

test("a slow earlier chat response cannot highlight the wrong chat", async () => {
  tour.completed = true;
  knowledgeChats = [
    { id: "chat-one", title: "First chat", messages: [] },
    { id: "chat-two", title: "Second chat", messages: [] }
  ];
  await window.tourTest.refreshConfig();
  await window.tourTest.loadChats();
  const originalFetch = window.fetch;
  let finishFirst;
  window.fetch = jest.fn((url, options) => url === "/api/org/chats/chat-one"
    ? new Promise(resolve => { finishFirst = resolve; }) : originalFetch(url, options));

  const first = window.tourTest.showChat("chat-one");
  await window.tourTest.showChat("chat-two");
  finishFirst(response({ chat: knowledgeChats[0], drafts: [] }));
  await first;

  expect(document.getElementById("chatTitle").textContent).toBe("Second chat");
  expect(document.querySelector('[data-chat-id="chat-two"]').getAttribute("aria-current")).toBe("page");
  expect(document.querySelector('[data-chat-id="chat-one"]').classList.contains("active")).toBe(false);
});

test("attaching from a knowledge chat moves to the file capable onboarding chat", async () => {
  tour.completed = true;
  knowledgeChats = [{ id: "chat-one", title: "Question", messages: [] }];
  await window.tourTest.refreshConfig();
  await window.tourTest.loadChats();
  await window.tourTest.showChat("chat-one");

  document.getElementById("attachButton").click();

  expect(document.getElementById("chatTitle").textContent).toBe("Acme onboarding");
  expect(document.querySelector('[data-chat-mode="discovery"]').getAttribute("aria-current")).toBe("page");
  expect(document.getElementById("status").textContent).toContain("supporting evidence");
});
