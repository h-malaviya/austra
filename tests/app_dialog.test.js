/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");

beforeEach(() => {
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  const app = fs.readFileSync(path.join(staticRoot, "app.js"), "utf8");
  window.eval(app.replace(/\nstart\(\);\s*$/, "\nwindow.testLoadChats = loadChats; window.testShowError = showError;"));
  window.confirm = jest.fn(() => { throw new Error("Native confirm must not open"); });
  window.alert = jest.fn(() => { throw new Error("Native alert must not open"); });
});

afterEach(() => {
  delete window.testLoadChats;
  delete window.testShowError;
});

async function openDeleteDialog() {
  window.fetch = jest.fn(async (url, options) => ({
    ok: true,
    json: async () => url === "/api/chats" && !options?.method
      ? [{ id: "chat-1", title: "Quartz suppliers" }]
      : {}
  }));
  await window.testLoadChats();
  document.querySelector(".chat-menu-trigger").click();
  document.querySelector(".chat-menu-delete").click();
  return document.querySelector('[role="dialog"]');
}

test("chat deletion uses a focused site modal and Escape cancels without deleting", async () => {
  const dialog = await openDeleteDialog();
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(document.querySelector(".app-shell").hasAttribute("inert")).toBe(true);
  expect(dialog.textContent).toContain("Quartz suppliers");
  expect(document.activeElement.textContent).toBe("Cancel");

  dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await Promise.resolve();

  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.querySelector(".app-shell").hasAttribute("inert")).toBe(false);
  expect(document.activeElement).toBe(document.querySelector(".chat-menu-trigger"));
  expect(window.fetch).not.toHaveBeenCalledWith("/api/chats/chat-1", expect.anything());
  expect(window.confirm).not.toHaveBeenCalled();
});

test("chat is deleted only after the site modal is confirmed", async () => {
  const dialog = await openDeleteDialog();
  expect(window.fetch).not.toHaveBeenCalledWith("/api/chats/chat-1", expect.anything());
  [...dialog.querySelectorAll("button")].find(button => button.textContent === "Delete chat").click();
  await new Promise(resolve => setTimeout(resolve, 0));

  expect(window.fetch).toHaveBeenCalledWith("/api/chats/chat-1", expect.objectContaining({ method: "DELETE" }));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(window.confirm).not.toHaveBeenCalled();
});

test("errors use the same site modal instead of a browser alert", () => {
  window.testShowError("Could not save the chat.");
  const dialog = document.querySelector('[role="dialog"]');
  expect(dialog.textContent).toContain("Could not save the chat.");
  expect(document.getElementById("status").textContent).toBe("Could not save the chat.");
  dialog.querySelector("button").click();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(window.alert).not.toHaveBeenCalled();
});
