let currentChatId = null;
let currentConfig = null;
let pendingFile = null;
let busy = false;

const byId = id => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
  return data;
}

function add(parent, tag, text = "", className = "") {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  parent.appendChild(item);
  return item;
}

function status(text, error = false) {
  byId("status").textContent = text;
  byId("status").className = error ? "status error" : "status";
}

function thread() {
  let item = byId("thread");
  if (!item) {
    item = add(byId("messages"), "div", "", "thread");
    item.id = "thread";
  }
  return item;
}

function scrollBottom() {
  const messages = byId("messages");
  messages.scrollTop = messages.scrollHeight;
  requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
}

function bubble(text, role = "assistant") {
  const row = add(thread(), "div", "", `message-row ${role}`);
  const item = add(row, "div", "", "bubble");
  if (role === "assistant") add(item, "div", "Austra", "assistant-label");
  add(item, "p", text, "assistant-text");
  scrollBottom();
  return item;
}

function renderWelcome() {
  currentChatId = null;
  byId("chatTitle").textContent = "New chat";
  byId("messages").replaceChildren();
  const welcome = add(byId("messages"), "div", "", "welcome");
  add(welcome, "div", "✦", "welcome-icon");
  add(welcome, "h1", "Start with a blank sheet");
  add(welcome, "p", "Describe your business in chat. Austra will ask for missing facts and propose skills, rules, and structures for your review.");
  thread();
}

async function refreshConfig() {
  currentConfig = await api("/api/config");
  byId("connection").textContent = currentConfig.connectors.anthropic
    ? "Claude key saved · checking" : "Claude setup needed";
  byId("connection").classList.remove("ready");
}

function connectionStatus(connected) {
  byId("connection").textContent = connected ? "Claude connected" : "Claude connection failed";
  byId("connection").classList.toggle("ready", connected);
}

function showError(message) {
  status(message, true);
  window.alert(message);
}

async function testSavedClaude() {
  try {
    await api("/api/credentials/anthropic/test", { method: "POST" });
    connectionStatus(true);
    status("Claude connection verified.");
  } catch (error) {
    connectionStatus(false);
    showError(error.message);
    securePrompt("anthropic");
  }
}

function securePrompt(connector) {
  const guide = currentConfig.guides[connector];
  const missing = connector === "anthropic"
    ? ["ANTHROPIC_API_KEY"] : currentConfig.missing[connector];
  const card = bubble(`Connect ${guide.title}. ${guide.steps}`);
  const link = add(card, "a", "Open official setup guide ↗", "guide-link");
  link.href = guide.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (!missing.length) {
    add(card, "p", "Already configured.");
    return;
  }
  if (connector === "anthropic" && currentConfig.connectors.anthropic) {
    add(card, "p", "A key is saved. Enter a new key here to replace it, or test the saved key.");
    const test = add(card, "button", "Test saved connection", "inline-button");
    test.type = "button";
    test.onclick = async () => {
      try {
        await api("/api/credentials/anthropic/test", { method: "POST" });
        connectionStatus(true);
        status("Claude connection verified.");
      } catch (error) {
        connectionStatus(false);
        showError(error.message);
      }
    };
  }
  const inputs = new Map();
  missing.forEach(name => {
    add(card, "label", name, "secure-label");
    const input = add(card, "input", "", "secure-input");
    input.type = name.endsWith("EMAIL") || name.endsWith("TEAM_ID") ? "text" : "password";
    input.autocomplete = "off";
    inputs.set(name, input);
  });
  const save = add(card, "button", "Save privately", "inline-button");
  save.type = "button";
  save.onclick = async () => {
    const values = Object.fromEntries([...inputs].map(([name, input]) => [name, input.value.trim()]));
    if (Object.values(values).some(value => !value)) {
      showError("Complete each connection value before saving.");
      return;
    }
    try {
      save.disabled = true;
      save.textContent = connector === "anthropic" ? "Testing connection…" : "Saving…";
      await api(`/api/credentials/${connector}`, {
        method: "POST", body: JSON.stringify({ values })
      });
      inputs.forEach(input => { input.value = ""; input.remove(); });
      save.remove();
      await refreshConfig();
      if (connector === "anthropic") connectionStatus(true);
      status(`${guide.title} ${connector === "anthropic" ? "connection verified and key" : "credentials"} saved privately.`);
      if (connector === "anthropic" && currentChatId) {
        status("Claude is continuing your chat…");
        const result = await api(`/api/chats/${currentChatId}/retry`, { method: "POST" });
        renderChat(result);
        if (!result.analysis_error) status("Claude replied.");
      }
    } catch (error) {
      showError(error.message);
    } finally {
      save.disabled = false;
      save.textContent = "Save privately";
    }
  };
  scrollBottom();
}

function accessPrompt(setup) {
  byId("messages").replaceChildren();
  thread();
  const card = bubble(setup
    ? "Create your administrator passphrase to begin. This protects business chats, draft approvals, and connector credentials."
    : "Enter your administrator passphrase to continue.");
  const input = add(card, "input", "", "secure-input");
  input.type = "password";
  input.autocomplete = setup ? "new-password" : "current-password";
  input.placeholder = setup ? "At least 12 characters" : "Administrator passphrase";
  const button = add(card, "button", setup ? "Create workspace" : "Sign in", "inline-button");
  button.type = "button";
  button.onclick = async () => {
    try {
      await api(setup ? "/api/setup" : "/api/login", {
        method: "POST", body: JSON.stringify({ password: input.value })
      });
      input.value = "";
      await refreshConfig();
      renderWelcome();
      await loadChats();
      if (!currentConfig.connectors.anthropic) securePrompt("anthropic");
      else await testSavedClaude();
    } catch (error) {
      status(error.message, true);
    }
  };
  input.onkeydown = event => { if (event.key === "Enter") button.click(); };
  input.focus();
}

async function loadChats() {
  const chats = await api("/api/chats");
  const list = byId("chatList");
  list.replaceChildren();
  if (!chats.length) add(list, "p", "No chats yet.", "connection");
  chats.forEach(chat => {
    const row = add(list, "div", "", "chat-list-item");
    const button = add(row, "button", chat.title, `chat-link${chat.id === currentChatId ? " active" : ""}`);
    button.type = "button";
    button.onclick = () => showChat(chat.id);
    const remove = add(row, "button", "×", "delete-chat");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete chat ${chat.title}`);
    remove.title = "Delete chat (memory stays)";
    remove.onclick = async () => {
      if (!window.confirm("Delete this chat from the chat list? Its memory and approved files remain.")) return;
      try {
        await api(`/api/chats/${chat.id}`, { method: "DELETE" });
        if (currentChatId === chat.id) renderWelcome();
        await loadChats();
        status("Chat deleted. Memory and approved files remain.");
      } catch (error) { status(error.message, true); }
    };
  });
}

function renderDraft(item) {
  const card = add(thread(), "div", "", "review-card");
  add(card, "strong", `${item.kind} · ${item.name}`);
  add(card, "p", item.summary);
  add(card, "pre", item.content, "skill-source");
  add(card, "small", item.status);
  if (item.status !== "proposed") return;
  const actions = add(card, "div", "", "card-actions");
  ["approved", "rejected"].forEach(decision => {
    const button = add(actions, "button", decision === "approved" ? "Approve" : "Reject",
      decision === "rejected" ? "reject" : "");
    button.type = "button";
    button.onclick = async () => {
      try {
        await api(`/api/drafts/${item.id}/review`, {
          method: "POST", body: JSON.stringify({ decision })
        });
        await showChat(currentChatId);
        status(`Draft ${decision}.`);
      } catch (error) { status(error.message, true); }
    };
  });
}

function renderChat(result) {
  const chat = result.chat;
  currentChatId = chat.id;
  byId("chatTitle").textContent = chat.title;
  byId("messages").replaceChildren();
  chat.messages.forEach(message => bubble(message.body, message.role));
  (result.drafts || []).forEach(renderDraft);
  (result.credential_requests || []).forEach(name => {
    if (!currentConfig.connectors[name]) securePrompt(name);
  });
  if (result.analysis_error) showError(result.analysis_error);
  scrollBottom();
  loadChats().catch(error => status(error.message, true));
}

async function showChat(chatId) {
  try {
    renderChat(await api(`/api/chats/${chatId}`));
    byId("sidebar").classList.remove("open");
  } catch (error) { status(error.message, true); }
}

async function sendMessage() {
  if (busy || !currentConfig?.authenticated) return;
  const input = byId("messageInput");
  const text = input.value.trim();
  if (!text && !pendingFile) return;
  if (/sk-ant-|xox[baprs]-|-----BEGIN .*PRIVATE KEY-----/i.test(text)) {
    status("Use Connections to enter credentials privately.", true);
    return;
  }
  busy = true;
  byId("sendButton").disabled = true;
  status("Claude is working on your request…");
  input.value = "";
  if (text) bubble(text, "user");
  scrollBottom();
  let accepted = false;
  try {
    let result;
    if (currentChatId) {
      result = text
        ? await api(`/api/chats/${currentChatId}/messages`, {
          method: "POST", body: JSON.stringify({ body: text })
        })
        : await api(`/api/chats/${currentChatId}`);
    } else {
      result = await api("/api/chats", {
        method: "POST", body: JSON.stringify({ body: text || `Please read ${pendingFile.name}` })
      });
    }
    accepted = true;
    renderChat(result);
    if (pendingFile) {
      const form = new FormData();
      form.append("upload", pendingFile);
      const response = await fetch(`/api/chats/${currentChatId}/attachments`, {
        method: "POST", credentials: "same-origin", body: form
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Attachment failed");
      result = data;
      renderChat(data);
      pendingFile = null;
      byId("fileInput").value = "";
      byId("attachmentPreview").hidden = true;
    }
    if (!result.analysis_error) status("Claude replied.");
  } catch (error) {
    if (!accepted) input.value = text;
    showError(error.message);
  } finally {
    busy = false;
    byId("sendButton").disabled = false;
    input.focus();
  }
}

function showConnections() {
  if (!currentConfig?.authenticated) return;
  const card = bubble("Choose a connection. Values are saved in a private local .env file and never added to chat.");
  const actions = add(card, "div", "", "card-actions");
  Object.keys(currentConfig.guides).forEach(name => {
    const button = add(actions, "button", currentConfig.guides[name].title);
    button.type = "button";
    button.onclick = () => securePrompt(name);
  });
}

async function showWorkspace() {
  try {
    const [artifacts, records] = await Promise.all([api("/api/artifacts"), api("/api/records")]);
    const files = artifacts.map(item => `${item.kind}/${item.name} · version ${item.version}`);
    const saved = records.map(item => `record/${item.schema} · ${item.id.slice(0, 8)}`);
    bubble(files.length || saved.length
      ? [...files, ...saved].join("\n")
      : "The business workspace is empty. Describe a rule or process in chat to draft its first file.");
  } catch (error) { status(error.message, true); }
}

async function showInfrastructure() {
  try {
    const result = await api("/api/infrastructure");
    const lines = Object.entries(result.services).map(([name, state]) => `${name}: ${state}`);
    bubble(`Optional Compose services\n${lines.join("\n")}\n\nAsk Austra to propose a start or stop change. Approve its infrastructure draft to apply it.`);
  } catch (error) { status(error.message, true); }
}

async function showAudit() {
  try {
    const events = await api("/api/audit");
    const lines = events.map(event => {
      const skills = event.skills_used.length ? ` · skills: ${event.skills_used.join(", ")}` : "";
      const detail = Object.entries(event.details).map(([key, value]) => `${key}=${value}`).join(" ");
      const source = event.chat_id ? ` · chat=${event.chat_id}` : "";
      const draft = event.draft_id ? ` · draft=${event.draft_id}` : "";
      return `${event.at} · ${event.actor} · ${event.action} · ${event.target_kind}/${event.target_name} · ${event.outcome}${skills}${source}${draft}${detail ? ` · ${detail}` : ""}`;
    });
    bubble(lines.length ? `Recent audit events\n${lines.join("\n")}` : "No audit events yet.");
  } catch (error) { status(error.message, true); }
}

byId("newChat").onclick = () => { if (currentConfig?.authenticated) renderWelcome(); };
byId("sendButton").onclick = sendMessage;
byId("messageInput").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
};
byId("attachButton").onclick = () => byId("fileInput").click();
byId("fileInput").onchange = () => {
  pendingFile = byId("fileInput").files[0] || null;
  byId("attachmentPreview").textContent = pendingFile ? `📎 ${pendingFile.name}` : "";
  byId("attachmentPreview").hidden = !pendingFile;
};
byId("connectionsButton").onclick = showConnections;
byId("topConnections").onclick = showConnections;
byId("workspaceButton").onclick = showWorkspace;
byId("infrastructureButton").onclick = showInfrastructure;
byId("auditButton").onclick = showAudit;
byId("menuButton").onclick = () => byId("sidebar").classList.toggle("open");
byId("logoutButton").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  await refreshConfig();
  currentChatId = null;
  accessPrompt(false);
};

async function start() {
  try {
    await refreshConfig();
    if (!currentConfig.admin_ready) accessPrompt(true);
    else if (!currentConfig.authenticated) accessPrompt(false);
    else {
      renderWelcome();
      await loadChats();
      if (!currentConfig.connectors.anthropic) securePrompt("anthropic");
      else await testSavedClaude();
    }
  } catch (error) { status(error.message, true); }
}

start();
