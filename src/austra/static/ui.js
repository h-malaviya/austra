let selectedCaseId = null;
let modelConfigured = false;
let managerConfigured = false;
let gmailConfigured = false;
let pendingFile = null;
let fileMessageSavedForCaseId = null;
let guided = null;
let busy = false;
let definitions = [];

const byId = id => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  return data;
}

function add(parent, tag, value, className = "") {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  parent.appendChild(element);
  return element;
}

function status(message, isError = false) {
  const element = byId("status");
  element.textContent = message;
  element.className = isError ? "status error" : "status";
}

function openDialog(id) {
  byId(id).hidden = false;
}

function closeDialog(id) {
  byId(id).hidden = true;
}

function manager() {
  const name = byId("managerName").value.trim();
  const token = byId("managerToken").value;
  if (!managerConfigured) throw new Error("Manager access is not configured on the server yet.");
  if (!name || !token) {
    openDialog("accessDialog");
    throw new Error("Enter your manager name and token first.");
  }
  return { name, headers: { "X-Manager-Token": token } };
}

function assistantBubble(text) {
  const thread = byId("thread");
  const row = add(thread, "div", "", "message-row assistant");
  const bubble = add(row, "div", "", "bubble");
  const label = add(bubble, "div", "", "assistant-label");
  add(label, "span", "A", "mini-mark");
  add(label, "span", "Austra");
  add(bubble, "p", text, "assistant-text");
  return bubble;
}

function localNote(text) {
  if (!byId("thread")) renderWelcome();
  byId("messages").querySelector(".welcome")?.remove();
  assistantBubble(text);
  scrollBottom();
}

function localUser(text) {
  const row = add(byId("thread"), "div", "", "message-row user");
  add(row, "div", text, "bubble");
}

function scrollBottom() {
  const container = byId("messages");
  container.scrollTop = container.scrollHeight;
}

function renderWelcome() {
  selectedCaseId = null;
  byId("chatTitle").textContent = "New chat";
  byId("chatSubtitle").textContent = "Purchasing · Sales · Inventory · Stock";
  const container = byId("messages");
  container.replaceChildren();
  const welcome = add(container, "div", "", "welcome");
  add(welcome, "div", "✦", "welcome-icon");
  add(welcome, "h1", "What can I help with today?");
  add(welcome, "p", "Ask about a purchase, sale, supplier, or stock situation. Each conversation stays in your chat history.");
  const suggestions = add(welcome, "div", "", "suggestions");
  ["I need to buy quartz", "Check a sales order", "Help me understand stock"].forEach(example => {
    const button = add(suggestions, "button", example);
    button.type = "button";
    button.onclick = () => { byId("messageInput").value = example; byId("messageInput").focus(); resizeComposer(); };
  });
  add(container, "div", "", "thread").id = "thread";
}

async function loadChats() {
  const cases = await api("/api/cases");
  const list = byId("chatList");
  list.replaceChildren();
  if (!cases.length) add(list, "p", "Your conversations will appear here.", "connection");
  cases.forEach(item => {
    const button = add(list, "button", "", `chat-link${item.id === selectedCaseId ? " active" : ""}`);
    button.type = "button";
    add(button, "strong", item.summary || item.request_text);
    add(button, "small", `${item.category || "Conversation"} · ${item.status}`);
    button.onclick = () => showChat(item.id);
  });
}

function renderMessage(thread, message) {
  const row = add(thread, "div", "", "message-row user");
  const bubble = add(row, "div", message.body, "bubble");
  bubble.title = `${message.sender} · ${message.channel}`;
}

function renderDocument(bubble, document) {
  const chip = add(bubble, "span", "", "file-chip");
  add(chip, "span", `📎 ${document.file_name} · ${document.status}`);
  const preview = add(chip, "button", "Preview");
  preview.type = "button";
  preview.onclick = async () => {
    try {
      const access = manager();
      const result = await api(`/api/admin/documents/${document.id}`, { headers: access.headers });
      localNote(result.extracted_text?.slice(0, 4000) || "No text was extracted. This file needs text review or OCR.");
    } catch (error) { status(error.message, true); }
  };
}

function reviewCard(bubble, title, description, state, onApprove, onReject) {
  const card = add(bubble, "div", "", "review-card");
  const heading = add(card, "strong", title);
  add(heading, "span", state, "badge");
  if (description) add(card, "p", description);
  if (state === "proposed") {
    const actions = add(card, "div", "", "card-actions");
    const approve = add(actions, "button", "Approve");
    const reject = add(actions, "button", "Reject", "reject");
    approve.type = reject.type = "button";
    approve.onclick = onApprove;
    reject.onclick = onReject;
  }
}

function renderPlan(thread, item) {
  if (!item.summary && !item.actions.length && !item.records.length && !item.skill_drafts.length && !item.memory_drafts.length && !item.system_changes.length && !item.policy_drafts.length && !item.supplier_drafts.length && !item.purchase_orders.length && !item.stock_receipts.length && !item.sales_orders.length && !item.email_outbox.length) {
    const bubble = assistantBubble("This chat is saved. Connect the Anthropic API key to let me analyze it and suggest next steps.");
    if (modelConfigured) bubble.lastChild.textContent = "This chat is saved. Send another message to request a plan.";
    return;
  }
  const bubble = assistantBubble(item.summary || "I have a plan for this case.");
  const planEvent = [...item.events].reverse().find(event => event.kind === "plan_proposed");
  const missing = planEvent?.details?.missing_information || [];
  if (missing.length) add(bubble, "p", `I still need: ${missing.join(" · ")}`, "assistant-note");
  if (item.actions.length) add(bubble, "div", "Suggested next steps", "section-title");
  item.actions.forEach(action => reviewCard(
    bubble, action.description, action.reason, action.status,
    () => review("actions", action.id, "approved"),
    () => review("actions", action.id, "rejected")
  ));
  if (item.records.length) add(bubble, "div", "Suggested record types", "section-title");
  item.records.forEach(record => reviewCard(
    bubble, record.name, `${record.description} · ${record.fields.map(field => field.name).join(", ")}`,
    record.status,
    () => review("records", record.id, "approved"),
    () => review("records", record.id, "rejected")
  ));
  if (item.skill_drafts.length) add(bubble, "div", "Suggested skills", "section-title");
  item.skill_drafts.forEach(draft => {
    reviewCard(bubble, draft.name, draft.reason, draft.status,
      () => review("skills", draft.id, "approved"),
      () => review("skills", draft.id, "rejected"));
    add(bubble, "pre", draft.diff || draft.body, "skill-source");
  });
  if (item.memory_drafts.length) add(bubble, "div", "Suggested memory", "section-title");
  item.memory_drafts.forEach(draft => reviewCard(
    bubble, "Remember this", `${draft.content} · ${draft.reason} · Source case: ${draft.case_id}`,
    draft.status,
    () => review("memory", draft.id, "approved"),
    () => review("memory", draft.id, "rejected")
  ));
  if (item.system_changes.length) add(bubble, "div", "System change proposals", "section-title");
  item.system_changes.forEach(change => {
    reviewCard(bubble, `${change.area}: ${change.title}`, change.reason, change.status,
      () => review("system", change.id, "approved"),
      () => review("system", change.id, "rejected"));
    add(bubble, "pre", change.specification, "skill-source");
  });
  if (item.policy_drafts.length) add(bubble, "div", "Policy drafts", "section-title");
  item.policy_drafts.forEach(draft => {
    reviewCard(bubble, draft.title, draft.reason, draft.status,
      () => review("policies", draft.id, "approved"),
      () => review("policies", draft.id, "rejected"));
    add(bubble, "pre", draft.body, "skill-source");
  });
  if (item.supplier_drafts.length) add(bubble, "div", "Supplier verification", "section-title");
  item.supplier_drafts.forEach(supplier => reviewCard(
    bubble, supplier.name, `${supplier.email} · ${supplier.material} · ${supplier.reason}`,
    supplier.status,
    () => review("suppliers", supplier.id, "approved"),
    () => review("suppliers", supplier.id, "rejected")
  ));
  if (item.purchase_orders.length) add(bubble, "div", "Purchase orders", "section-title");
  item.purchase_orders.forEach(order => reviewCard(
    bubble, `${order.material} · ${order.quantity_tonnes} t`,
    `Supplier ID: ${order.supplier_id} · Grade: ${order.grade} · USD ${order.unit_price_usd}/t · Total USD ${order.total_usd} · Payment: ${order.payment_terms} · Delivery: ${order.delivery_terms} · Freight: ${order.freight_terms} · Tax: ${order.tax_terms}`,
    order.status,
    () => review("purchase_orders", order.id, "approved"),
    () => review("purchase_orders", order.id, "rejected")
  ));
  if (item.stock_receipts.length) add(bubble, "div", "Stock receipts", "section-title");
  item.stock_receipts.forEach(receipt => reviewCard(
    bubble, `${receipt.item} · ${receipt.quantity_tonnes} t`,
    `${receipt.item_kind} · Lot ${receipt.lot_code} · Grade ${receipt.grade} · ${receipt.location}`,
    receipt.status,
    () => review("stock_receipts", receipt.id, "approved"),
    () => review("stock_receipts", receipt.id, "rejected")
  ));
  if (item.sales_orders.length) add(bubble, "div", "Sales orders", "section-title");
  item.sales_orders.forEach(order => reviewCard(
    bubble, `${order.customer} · ${order.quantity_tonnes} t ${order.item}`,
    `Available verified finished stock: ${order.available_tonnes} t · ${order.reason}`,
    order.status,
    () => review("sales_orders", order.id, "approved"),
    () => review("sales_orders", order.id, "rejected")
  ));
  if (item.email_outbox.length) add(bubble, "div", "PO emails", "section-title");
  item.email_outbox.forEach(draft => {
    const card = add(bubble, "div", "", "review-card");
    const heading = add(card, "strong", draft.subject);
    add(heading, "span", draft.status, "badge");
    add(card, "p", `To: ${draft.recipient}`);
    add(card, "pre", draft.body, "skill-source");
    if (draft.status === "ready") {
      const button = add(card, "button", gmailConfigured ? "Send PO email" : "Connect Gmail to send", "inline-button");
      button.type = "button";
      button.disabled = !gmailConfigured;
      button.onclick = () => sendPoEmail(draft.po_id);
    }
    if (draft.status === "sent") {
      const button = add(card, "button", "Check replies", "inline-button");
      button.type = "button";
      button.onclick = () => syncPoEmail(draft.po_id);
    }
  });
  if (item.business_records.length) {
    add(bubble, "div", "Case data", "section-title");
    item.business_records.forEach(record => {
      const definition = definitions.find(entry => entry.id === record.definition_id);
      reviewCard(bubble, definition?.name || "Record", Object.entries(record.values).map(([key, value]) => `${key}: ${value}`).join(" · "), record.status);
    });
  }
  add(bubble, "p", "Supplier and stock approvals verify those records. Sales approval reserves available finished stock. PO approval does not email the PO; system approval does not deploy code.", "assistant-note");
}

async function showChat(id) {
  try {
    const item = await api(`/api/cases/${id}`);
    selectedCaseId = id;
    guided = null;
    byId("chatTitle").textContent = item.summary || item.request_text;
    byId("chatSubtitle").textContent = `${item.category || "Conversation"} · ${item.status}`;
    const container = byId("messages");
    container.replaceChildren();
    const thread = add(container, "div", "", "thread");
    thread.id = "thread";
    item.messages.forEach(message => renderMessage(thread, message));
    if (item.documents.length) {
      const bubble = assistantBubble("Documents attached to this chat:");
      item.documents.forEach(document => renderDocument(bubble, document));
    }
    renderPlan(thread, item);
    await loadChats();
    byId("sidebar").classList.remove("open");
    scrollBottom();
  } catch (error) { status(error.message, true); }
}

async function review(type, id, decision) {
  try {
    const access = manager();
    await api(`/api/proposals/${type}/${id}/review`, {
      method: "POST", headers: access.headers,
      body: JSON.stringify({ decision, manager: access.name })
    });
    if (type === "records") definitions = await api("/api/definitions");
    status(`Proposal ${decision}. No email or PO was sent.`);
    await showChat(selectedCaseId);
  } catch (error) { status(error.message, true); }
}

async function sendPoEmail(poId) {
  try {
    const access = manager();
    await api(`/api/purchase-orders/${poId}/send`, {
      method: "POST", headers: access.headers
    });
    status("PO email sent. The Gmail thread is recorded with this chat.");
    await showChat(selectedCaseId);
  } catch (error) { status(error.message, true); }
}

async function syncPoEmail(poId) {
  try {
    const access = manager();
    const result = await api(`/api/purchase-orders/${poId}/sync`, {
      method: "POST", headers: access.headers
    });
    status(`${result.new_messages} new supplier replies added to this chat.`);
    await showChat(selectedCaseId);
  } catch (error) { status(error.message, true); }
}

async function analyzeChat(id) {
  status("Austra is preparing a plan…");
  try {
    await api(`/api/cases/${id}/analyze`, { method: "POST" });
    status("Plan ready for review.");
  } catch (error) {
    status(`Chat saved. Analysis could not finish: ${error.message}`, true);
  }
}

async function uploadFile(caseId, file) {
  const access = manager();
  const form = new FormData();
  form.append("file", file);
  form.append("uploaded_by", access.name);
  form.append("case_id", caseId);
  const response = await fetch("/api/admin/documents", {
    method: "POST", headers: access.headers, body: form
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Upload failed");
  return result;
}

function resetAttachment() {
  pendingFile = null;
  fileMessageSavedForCaseId = null;
  byId("fileInput").value = "";
  byId("attachmentPreview").hidden = true;
  byId("attachmentPreview").replaceChildren();
}

function resizeComposer() {
  const input = byId("messageInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}
