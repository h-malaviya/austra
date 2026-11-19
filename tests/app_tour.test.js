/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder } = require("node:util");

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
    /\nstart\(\);\s*$/, "\nwindow.tourTest = { refreshConfig, renderTour, renderWelcome, sendMessage, showChat, showSignedOut };"
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
    messages: [], website_status: null
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
      const { body, revision } = JSON.parse(options.body);
      if (revision !== tour.revision) return response({ detail: "Stale conversation" }, 409);
      tour.messages.push({ role: "user", body });
      tour.messages.push({ role: "assistant", body: "Who follows up with the client?" });
      tour.revision += 1;
      return streamResponse(tour);
    }
    if (url === "/api/org/chats") {
      return response(knowledgeChats.map(chat => ({ id: chat.id, title: chat.title })));
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
  await reply("We need help following up with new clients.");
  expect(document.getElementById("messages").textContent).toContain("Who follows up");
  expect(window.fetch).toHaveBeenCalledWith("/api/org/discovery/message/stream", expect.anything());
  expect(window.fetch).not.toHaveBeenCalledWith("/api/org/ask/stream", expect.anything());
  [...document.querySelectorAll(".organization-chat-modes button")]
    .find(button => button.textContent === "Ask saved knowledge").click();
  await reply("What information is approved?");
  expect(window.fetch).toHaveBeenCalledWith("/api/org/ask/stream", expect.anything());
});

test("profile stays available during the tour", async () => {
  await window.tourTest.refreshConfig();
  window.tourTest.renderTour();
  document.getElementById("profileButton").click();
  expect(document.getElementById("accountContent").textContent).toContain("Ava");
  expect(document.getElementById("accountContent").textContent).toContain("Acme");
  expect(document.getElementById("departmentsButton").hidden).toBe(true);
});

test("knowledge chats stay visible and continue by chat id", async () => {
  tour.completed = true;
  config.connectors.anthropic = true;
  await window.tourTest.refreshConfig();
  window.tourTest.renderWelcome();
  expect(document.getElementById("chatList").hidden).toBe(false);
  [...document.querySelectorAll(".organization-chat-modes button")]
    .find(button => button.textContent === "Ask saved knowledge").click();
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
