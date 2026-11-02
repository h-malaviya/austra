/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");
const fullAnswer = "Full answer with 122.89725 t quartzite.\n\n| Order | Need |\n| --- | ---: |\n| SO-1 | 8.5 |";

beforeEach(() => {
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.requestAnimationFrame = callback => callback();
  window.eval(fs.readFileSync(path.join(staticRoot, "markdown.js"), "utf8"));
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/, "\nwindow.testRenderChat = renderChat;"));
});

function chat() {
  return {
    chat: {
      id: "chat-test",
      title: "Stock check",
      messages: [{
        id: "answer-test",
        role: "assistant",
        report: {},
        display_body: "Short verdict.",
        body: fullAnswer
      }]
    },
    drafts: []
  };
}

test("full answer is available before the visual surface loads", () => {
  window.AustraA2UI = { mount: jest.fn() };
  window.fetch = jest.fn(path => path === "/api/ui/chats/chat-test"
    ? new Promise(() => {})
    : Promise.resolve({ ok: true, json: async () => [] }));

  window.testRenderChat(chat());

  const bubble = document.querySelector(".bubble");
  expect(bubble.textContent).toContain("Short verdict.");
  expect(bubble.querySelector("details summary").textContent).toBe("View full answer");
  expect(bubble.querySelector("details .markdown-content").textContent)
    .toContain("Full answer with 122.89725 t quartzite.");
  expect(bubble.querySelector("details table")).not.toBeNull();
});

test("a complete visual report removes its duplicate source table after mounting", async () => {
  window.AustraA2UI = { mount: jest.fn() };
  window.fetch = jest.fn(path => Promise.resolve({
    ok: true,
    json: async () => path === "/api/ui/chats/chat-test"
      ? { surfaces: [{ id: "report-test", kind: "report", message_id: "answer-test", visual_complete: true }] }
      : []
  }));

  window.testRenderChat(chat());
  await new Promise(resolve => setTimeout(resolve, 0));

  const bubble = document.querySelector(".bubble");
  expect(window.AustraA2UI.mount).toHaveBeenCalledTimes(1);
  expect(bubble.querySelector(".a2ui-source")).toBeNull();
  expect(bubble.textContent).toContain("Short verdict.");
});

test("an incomplete visual report retains a clearly labeled source", async () => {
  window.AustraA2UI = { mount: jest.fn() };
  window.fetch = jest.fn(path => Promise.resolve({
    ok: true,
    json: async () => path === "/api/ui/chats/chat-test"
      ? { surfaces: [{ id: "report-test", kind: "report", message_id: "answer-test", visual_complete: false }] }
      : []
  }));

  window.testRenderChat(chat());
  await new Promise(resolve => setTimeout(resolve, 0));

  expect(document.querySelector(".bubble .a2ui-source summary").textContent)
    .toBe("View original text");
});

test("a failed visual mount restores the complete message", async () => {
  window.AustraA2UI = { mount: () => { throw new Error("Renderer failed"); } };
  window.fetch = jest.fn(path => Promise.resolve({
    ok: true,
    json: async () => path === "/api/ui/chats/chat-test"
      ? { surfaces: [{ id: "report-test", kind: "report", message_id: "answer-test" }] }
      : []
  }));

  window.testRenderChat(chat());
  await new Promise(resolve => setTimeout(resolve, 0));

  const bubble = document.querySelector(".bubble");
  expect(bubble.textContent).toContain("Full answer with 122.89725 t quartzite.");
  expect(bubble.querySelector("table")).not.toBeNull();
});

test("a missing renderer shows the complete message immediately", () => {
  delete window.AustraA2UI;
  window.fetch = jest.fn(() => Promise.resolve({ ok: true, json: async () => [] }));

  window.testRenderChat(chat());

  const bubble = document.querySelector(".bubble");
  expect(bubble.textContent).toContain("Full answer with 122.89725 t quartzite.");
  expect(bubble.querySelector("table")).not.toBeNull();
});
