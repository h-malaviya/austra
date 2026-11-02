/** @jest-environment jsdom */

const fs = require("node:fs");
const path = require("node:path");

beforeAll(() => {
  if (!document.adoptedStyleSheets) document.adoptedStyleSheets = [];
  if (!CSSStyleSheet.prototype.replaceSync) {
    CSSStyleSheet.prototype.replaceSync = function (text) {
      const style = document.createElement("style");
      style.textContent = text;
      document.head.appendChild(style);
    };
  }
  const bundle = fs.readFileSync(path.join(__dirname, "../src/austra/static/a2ui.bundle.js"), "utf8");
  window.eval(bundle);
});

afterEach(() => { document.body.replaceChildren(); });

function findDeep(root, selector) {
  const found = root.querySelector(selector);
  if (found) return found;
  for (const element of root.querySelectorAll("*")) {
    if (element.shadowRoot) {
      const nested = findDeep(element.shadowRoot, selector);
      if (nested) return nested;
    }
  }
  return null;
}

test("renders an A2UI draft and routes its review action", async () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  const actions = [];
  const description = {
    id: "draft_test",
    messages: [
      {
        version: "v0.9.1",
        createSurface: {
          surfaceId: "draft_test",
          catalogId: "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
        }
      },
      {
        version: "v0.9.1",
        updateComponents: {
          surfaceId: "draft_test",
          components: [
            { id: "title", component: "Text", text: "Policy · sample", variant: "h4" },
            { id: "label", component: "Text", text: "Approve", variant: "caption" },
            { id: "approve", component: "Button", child: "label", action: {
              event: { name: "approve_draft", context: { draft_id: "test" } }
            } },
            { id: "body", component: "Column", children: ["title", "approve"] },
            { id: "root", component: "Card", child: "body" }
          ]
        }
      }
    ]
  };
  window.AustraA2UI.mount(holder, description, action => actions.push(action));
  const host = holder.querySelector("austra-surface");
  await host.updateComplete;
  const surface = holder.querySelector("a2ui-surface");
  await surface.updateComplete;
  expect(holder.querySelector("a2ui-card")).not.toBeNull();
  await holder.querySelector("a2ui-basic-column").updateComplete;
  const button = findDeep(holder, "a2ui-basic-button");
  expect(button).not.toBeNull();
  await button.updateComplete;
  button.querySelector("button").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(actions[0].name).toBe("approve_draft");
  expect(actions[0].context.draft_id).toBe("test");
});

test("submits the answer entered in an A2UI text field", async () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  const actions = [];
  window.AustraA2UI.mount(holder, {
    id: "answer_test",
    messages: [
      { version: "v0.9.1", createSurface: {
        surfaceId: "answer_test",
        catalogId: "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
      } },
      { version: "v0.9.1", updateDataModel: {
        surfaceId: "answer_test", path: "/answer", value: ""
      } },
      { version: "v0.9.1", updateComponents: {
        surfaceId: "answer_test",
        components: [
          { id: "field", component: "TextField", label: "Your answer", value: { path: "/answer" } },
          { id: "label", component: "Text", text: "Send answer" },
          { id: "submit", component: "Button", child: "label", action: {
            event: { name: "submit_answer", context: { answer: { path: "/answer" } } }
          } },
          { id: "body", component: "Column", children: ["field", "submit"] },
          { id: "root", component: "Card", child: "body" }
        ]
      } }
    ]
  }, action => actions.push(action));
  await holder.querySelector("austra-surface").updateComplete;
  await holder.querySelector("a2ui-surface").updateComplete;
  await holder.querySelector("a2ui-card").updateComplete;
  await holder.querySelector("a2ui-basic-column").updateComplete;
  const field = holder.querySelector("a2ui-basic-textfield");
  await field.updateComplete;
  const input = field.querySelector("input");
  input.value = "Blue";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  const button = holder.querySelector("a2ui-basic-button");
  await button.updateComplete;
  button.querySelector("button").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(actions[0].name).toBe("submit_answer");
  expect(actions[0].context.answer).toBe("Blue");
});

test("renders an interactive report with switchable detail tabs", async () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  window.AustraA2UI.mount(holder, {
    id: "report_test",
    messages: [
      { version: "v0.9.1", createSurface: {
        surfaceId: "report_test",
        catalogId: "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
      } },
      { version: "v0.9.1", updateComponents: {
        surfaceId: "report_test",
        components: [
          { id: "overview_text", component: "Text", text: "Stock 1.5 t", variant: "body" },
          { id: "material_text", component: "Text", text: "Quartzite 122.897 t", variant: "body" },
          { id: "overview", component: "Column", children: ["overview_text"] },
          { id: "materials", component: "Column", children: ["material_text"] },
          { id: "root", component: "Tabs", tabs: [
            { title: "Overview", child: "overview" },
            { title: "Materials", child: "materials" }
          ] }
        ]
      } }
    ]
  }, () => {});
  await holder.querySelector("austra-surface").updateComplete;
  await holder.querySelector("a2ui-surface").updateComplete;
  const tabs = holder.querySelector("a2ui-tabs");
  expect(tabs).not.toBeNull();
  await tabs.updateComplete;
  await new Promise(resolve => setTimeout(resolve, 0));
  const buttons = tabs.querySelectorAll("button");
  expect(buttons.length).toBe(2);
  expect(holder.textContent).toContain("Stock 1.5 t");
  buttons[1].click();
  await tabs.updateComplete;
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(holder.textContent).toContain("Quartzite 122.897 t");
});

test("renders the report chart with proportional bars and selectable exact values", async () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  window.AustraA2UI.mount(holder, {
    id: "chart_test",
    messages: [
      { version: "v0.9.1", createSurface: {
        surfaceId: "chart_test", catalogId: "https://austra.local/catalogs/report.json"
      } },
      { version: "v0.9.1", updateComponents: {
        surfaceId: "chart_test", components: [{
          id: "root", component: "AustraBarChart", title: "Demand by order",
          mode: "stacked", unit: "t", rows: [
            { label: "SO-1", first: "1.5", second: "8.5", first_label: "Stock", second_label: "Produce" },
            { label: "SO-2", first: "0", second: "14", first_label: "Stock", second_label: "Produce" }
          ]
        }]
      } }
    ]
  }, () => {});
  await holder.querySelector("austra-surface").updateComplete;
  await holder.querySelector("a2ui-surface").updateComplete;
  const chart = holder.querySelector("austra-bar-chart");
  expect(chart).not.toBeNull();
  await chart.updateComplete;
  const bars = chart.shadowRoot.querySelectorAll(".bar");
  expect(bars[0].getAttribute("style")).toContain("10.714");
  expect(chart.shadowRoot.querySelector("[role=status]").textContent).toContain("1.5 t");
  chart.shadowRoot.querySelectorAll("button")[1].click();
  await chart.updateComplete;
  expect(chart.shadowRoot.querySelector("[role=status]").textContent).toContain("14 t");
  expect(chart.shadowRoot.querySelectorAll("button")[1].getAttribute("aria-pressed")).toBe("true");
});

test("shows inventory as a table and reveals row details on hover and focus", async () => {
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  const rows = [
    { label: "Quartzite (QZ)", first: "80.0", second: null, first_label: "Available (t)", second_label: "", details: [
      { label: "Lots", value: "4" }, { label: "On-hand (t)", value: "115.0" },
      { label: "Reserved (t)", value: "15.0" }, { label: "QC-hold (t)", value: "20.0" },
      { label: "Available (t)", value: "80.0" }
    ] },
    { label: "Charcoal (CH)", first: "8.0", second: null, first_label: "Available (t)", second_label: "", details: [
      { label: "Lots", value: "2" }, { label: "Available (t)", value: "8.0" }
    ] }
  ];
  window.AustraA2UI.mount(holder, {
    id: "inventory_test", messages: [
      { version: "v0.9.1", createSurface: {
        surfaceId: "inventory_test", catalogId: "https://austra.local/catalogs/report.json"
      } },
      { version: "v0.9.1", updateComponents: { surfaceId: "inventory_test", components: [
        { id: "chart", component: "AustraBarChart", title: "Available stock", mode: "single", unit: "t", rows },
        { id: "table", component: "AustraDataTable", title: "Current raw material", headers: ["Material", "Lots", "On-hand (t)", "Reserved (t)", "QC-hold (t)", "Available (t)"], rows: [
          ["Quartzite (QZ)", "4", "115.0", "15.0", "20.0", "80.0"],
          ["Charcoal (CH)", "2", "16.0", "2.0", "6.0", "8.0"]
        ] },
        { id: "root", component: "Column", children: ["chart", "table"] }
      ] } }
    ]
  }, () => {});
  await holder.querySelector("austra-surface").updateComplete;
  await holder.querySelector("a2ui-surface").updateComplete;
  const chart = holder.querySelector("austra-bar-chart");
  const table = holder.querySelector("austra-data-table");
  await chart.updateComplete;
  await table.updateComplete;
  expect(chart.shadowRoot.textContent).toContain("80.0 t");
  expect(table.shadowRoot.querySelectorAll("tbody tr")).toHaveLength(2);
  expect(table.shadowRoot.querySelectorAll("thead th")).toHaveLength(6);
  expect(table.shadowRoot.textContent).toContain("QC-hold (t)");
  expect(chart.shadowRoot.querySelector("[role=tooltip]")).toBeNull();
  chart.shadowRoot.querySelectorAll(".chart-row")[0].dispatchEvent(new MouseEvent("mouseenter"));
  await chart.updateComplete;
  const tooltip = chart.shadowRoot.querySelector("[role=tooltip]");
  expect(tooltip.textContent).toContain("Quartzite (QZ)");
  expect(tooltip.querySelectorAll("dt")).toHaveLength(5);
  chart.shadowRoot.querySelector("button").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  await chart.updateComplete;
  expect(chart.shadowRoot.querySelector("[role=tooltip]")).toBeNull();
  chart.shadowRoot.querySelectorAll("button")[1].dispatchEvent(new FocusEvent("focus"));
  await chart.updateComplete;
  expect(chart.shadowRoot.querySelector("[role=tooltip]").textContent).toContain("Charcoal (CH)");
});
