/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder, TextEncoder } = require("node:util");

const staticRoot = path.join(__dirname, "../src/austra/static");

beforeEach(() => {
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.TextDecoder = TextDecoder;
  window.requestAnimationFrame = callback => { callback(); return 1; };
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/, `
    window.streamTest = {
      postStream, renderChat,
      setConfig: value => { currentConfig = value; }
    };`));
  window.streamTest.setConfig({ authenticated: true, legacy_workspace: true, connectors: {} });
  window.fetch = jest.fn(async url => ({
    ok: true, status: 200,
    json: async () => url.startsWith("/api/ui/chats/") ? { surfaces: [] } : []
  }));
});

test("post stream reads split events and returns the completed result", async () => {
  const frames = 'event: delta\ndata: {"text":"Hel"}\n\nevent: delta\ndata: {"text":"lo"}\n\n'
    + 'event: done\ndata: {"answer":"Hello"}\n\n';
  const bytes = new TextEncoder().encode(frames);
  const pieces = [bytes.slice(0, 23), bytes.slice(23, 59), bytes.slice(59)];
  window.fetch = jest.fn(async () => ({
    ok: true, status: 200,
    body: { getReader: () => ({ read: async () => pieces.length
      ? { value: pieces.shift(), done: false } : { done: true } }) }
  }));
  const seen = [];

  const result = await window.streamTest.postStream("/api/org/ask/stream", { question: "Hi" },
    (event, data) => { if (event === "delta") seen.push(data.text); });

  expect(seen.join("")).toBe("Hello");
  expect(result).toEqual({ answer: "Hello" });
});

test("post stream rejects an interrupted response", async () => {
  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"Partial"}\n\n');
  let sent = false;
  window.fetch = jest.fn(async () => ({
    ok: true, status: 200,
    body: { getReader: () => ({ read: async () => {
      if (sent) return { done: true };
      sent = true;
      return { value: bytes, done: false };
    } }) }
  }));

  await expect(window.streamTest.postStream("/api/org/ask/stream", {}, () => {}))
    .rejects.toThrow("ended early");
});

test("final response appends to the existing chat without rebuilding old bubbles", () => {
  const user = { id: "message-1", role: "user", body: "Hello" };
  window.streamTest.renderChat({
    chat: { id: "chat-1", title: "Greeting", messages: [user] }, drafts: []
  });
  const original = document.querySelector(".bubble[data-message-id='message-1']");
  window.streamTest.renderChat({
    chat: { id: "chat-1", title: "Greeting", messages: [
      user, { id: "message-2", role: "assistant", body: "Hello back" }
    ] }, drafts: []
  });

  expect(document.querySelector(".bubble[data-message-id='message-1']")).toBe(original);
  expect(document.querySelectorAll(".message-row")).toHaveLength(2);
  expect(document.querySelector(".bubble[data-message-id='message-2']").textContent)
    .toContain("Hello back");
});
