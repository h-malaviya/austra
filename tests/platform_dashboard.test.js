/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

const staticRoot = path.join(__dirname, "../src/austra/static");
const organization = {
  id: "org-1", slug: "acme", name: "Acme Works", status: "active",
  email: "office@example.com", contact_number: "+1 555 0100",
  city: "Austin", country: "USA", admin_count: 1
};
const admin = {
  id: "admin-1", username: "owner", full_name: "Alex Owner",
  email: "alex@example.com", contact_number: "+1 555 0101", status: "active"
};

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  const html = fs.readFileSync(path.join(staticRoot, "index.html"), "utf8");
  document.body.innerHTML = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)[1];
  window.eval(fs.readFileSync(path.join(staticRoot, "app.js"), "utf8")
    .replace(/\nstart\(\);\s*$/, ""));
  window.eval(fs.readFileSync(path.join(staticRoot, "platform.js"), "utf8"));
  document.getElementById("platformRoot").hidden = false;
  window.fetch = jest.fn(async (url, options = {}) => {
    let data = {};
    if (url === "/api/platform/organizations" && !options.method) data = [organization];
    if (url === "/api/platform/organizations/org-1/admins" && !options.method) data = [admin];
    if (url.endsWith("/reset-password")) {
      data = { username: "owner", temporary_password: "temporary-secret" };
    }
    return { ok: true, status: 200, json: async () => data };
  });
});

test("company row opens admin details without exposing credentials", async () => {
  await window.renderPlatformDashboard();
  expect(document.querySelector(".platform-stat strong").textContent).toBe("1");
  document.querySelector('a[href="/platform/organizations"]').click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.pathname).toBe("/platform/organizations");
  expect(document.querySelector(".platform-search")).not.toBeNull();
  document.querySelector(".platform-org-name").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.pathname).toBe("/platform/organizations/org-1");
  expect(document.getElementById("platformDetail").textContent).toContain("Alex Owner");
  expect(document.getElementById("platformDetail").textContent).toContain("alex@example.com");
  expect(document.getElementById("platformDetail").textContent).toContain("Austin");
  expect(document.getElementById("platformDetail").textContent).toContain("USA");
  expect(document.getElementById("platformDetail").textContent).not.toContain("password_hash");

  [...document.querySelectorAll(".platform-text-button")]
    .find(button => button.textContent === "Reset password").click();
  [...document.querySelectorAll(".confirmation-dialog button")]
    .find(button => button.textContent === "Reset password").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.fetch).toHaveBeenCalledWith(
    "/api/platform/organizations/org-1/admins/admin-1/reset-password",
    expect.objectContaining({ method: "POST" })
  );
  expect(document.querySelector(".platform-credential").textContent).toBe("temporary-secret");
});

test("direct organization page load and back navigation show the right page", async () => {
  window.history.replaceState({}, "", "/platform/organizations/org-1");
  await window.renderPlatformDashboard();
  expect(document.getElementById("platformDetail").textContent).toContain("Alex Owner");
  document.querySelector(".platform-back-link").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.pathname).toBe("/platform/organizations");
  expect(document.querySelector(".platform-search")).not.toBeNull();
  const returned = new Promise(resolve => window.addEventListener("popstate", resolve, { once: true }));
  window.history.back();
  await returned;
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(window.location.pathname).toBe("/platform/organizations/org-1");
  expect(document.getElementById("platformDetail").textContent).toContain("Alex Owner");
});

test("test reset is visible only when enabled and requires the subdomain", async () => {
  organization.test_reset_enabled = true;
  try {
    window.history.replaceState({}, "", "/platform/organizations/org-1");
    await window.renderPlatformDashboard();
    [...document.querySelectorAll("#platformDetail button")]
      .find(button => button.textContent === "Reset test data").click();
    const field = document.querySelector('input[name="confirm_slug"]');
    expect(field.required).toBe(true);
    field.value = "acme";
    document.querySelector(".platform-modal form").dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true })
    );
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(window.fetch).toHaveBeenCalledWith(
      "/api/platform/organizations/org-1/test-reset",
      expect.objectContaining({ method: "POST", body: '{"confirm_slug":"acme"}' })
    );
  } finally {
    delete organization.test_reset_enabled;
  }
});

test("administrator modal actions have matching button alignment", () => {
  const style = document.createElement("style");
  style.textContent = fs.readFileSync(path.join(staticRoot, "app.css"), "utf8");
  document.head.appendChild(style);
  window.platformModal("Add administrator", "Enter account details.",
    [["Username", "username", "", true]], "Create admin", async () => {});
  const [cancel, create] = document.querySelectorAll(".platform-modal-actions button");
  expect(getComputedStyle(cancel).marginTop).toBe("0px");
  expect(getComputedStyle(create).marginTop).toBe("0px");
  expect(getComputedStyle(cancel).height).toBe(getComputedStyle(create).height);
  cancel.click();
  style.remove();
});
