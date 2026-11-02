/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");
let channels;

function response(data, status = 200) {
  return { ok: status < 400, status, json: async () => data };
}

beforeEach(() => {
  channels = [];
  window.BroadcastChannel = class {
    constructor() { channels.push(this); }
    postMessage = jest.fn();
  };
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.requestAnimationFrame = callback => callback();
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(
    /\nstart\(\);\s*$/,
    "\nwindow.authTest = { api, refreshConfig, showSignedOut, showModal };"
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
