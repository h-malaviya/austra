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
  byId("messages").scrollTop = byId("messages").scrollHeight;
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
  const connected = currentConfig.connectors.anthropic;
  byId("connection").textContent = connected ? "Claude connected" : "Claude setup needed";
  byId("connection").classList.toggle("ready", connected);
}

function securePrompt(connector) {
  const guide = currentConfig.guides[connector];
  const missing = currentConfig.missing[connector];
  const card = bubble(`Connect ${guide.title}. ${guide.steps}`);
  const link = add(card, "a", "Open official setup guide ↗", "guide-link");
  link.href = guide.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (!missing.length) {
    add(card, "p", "Already configured.");
    return;
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
      status("Complete each connection value before saving.", true);
      return;
    }
    try {
      await api(`/api/credentials/${connector}`, {
        method: "POST", body: JSON.stringify({ values })
      });
      inputs.forEach(input => { input.value = ""; input.remove(); });
      save.remove();
      await refreshConfig();
      status(`${guide.title} credentials saved in the private local environment file.`);
      if (connector === "anthropic" && currentChatId) {
        const result = await api(`/api/chats/${currentChatId}/retry`, { method: "POST" });
        renderChat(result);
      }
    } catch (error) {
      status(error.message, true);
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
    const button = add(list, "button", chat.title, `chat-link${chat.id === currentChatId ? " active" : ""}`);
    button.type = "button";
    button.onclick = () => showChat(chat.id);
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
    input.value = "";
    renderChat(result);
    if (pendingFile) {
      const form = new FormData();
      form.append("upload", pendingFile);
      const response = await fetch(`/api/chats/${currentChatId}/attachments`, {
        method: "POST", credentials: "same-origin", body: form
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Attachment failed");
      renderChat(data);
      pendingFile = null;
      byId("fileInput").value = "";
      byId("attachmentPreview").hidden = true;
    }
  } catch (error) {
    status(error.message, true);
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
    }
  } catch (error) { status(error.message, true); }
}

start();
