let currentChatId = null;
let currentConfig = null;
let pendingFiles = [];
let fileSelectionBusy = false;
let busy = false;
let activeJobIds = new Set();
let processPollTimer = null;
let sessionExpiryTimer = null;
let authGeneration = 0;
const authChannel = typeof window.BroadcastChannel === "function"
  ? new window.BroadcastChannel("austra-auth") : null;
let selectedConnector = "anthropic";
let workspaceSection = "records";
let workspaceOffset = 0;
let chatSurfaceRequest = 0;
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
const CHAT_TITLE_PREVIEW_CHARS = 22;

const byId = id => document.getElementById(id);

async function api(path, options = {}) {
  const requestGeneration = authGeneration;
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const data = await response.json();
  if (requestGeneration !== authGeneration) throw new Error("Your session ended. Sign in again.");
  if (response.status === 401 && path !== "/api/login" && path !== "/api/setup") {
    showSignedOut();
    throw new Error("Your session ended. Sign in again.");
  }
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

function showModal({ title, message, confirmLabel = "OK", cancelLabel, destructive = false }) {
  return new Promise(resolve => {
    const previousFocus = document.activeElement;
    const appShell = document.querySelector(".app-shell");
    const wasInert = appShell?.hasAttribute("inert");
    appShell?.setAttribute("inert", "");
    const backdrop = add(document.body, "div", "", "dialog-backdrop");
    const dialog = add(backdrop, "div", "", "dialog confirmation-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const heading = add(dialog, "h2", title);
    heading.id = "modal-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const description = add(dialog, "p", message);
    description.id = "modal-description";
    dialog.setAttribute("aria-describedby", description.id);
    const actions = add(dialog, "div", "", "dialog-actions");
    const cancel = cancelLabel ? add(actions, "button", cancelLabel, "top-action") : null;
    const confirm = add(actions, "button", confirmLabel, destructive ? "primary danger" : "primary");
    if (cancel) cancel.type = "button";
    confirm.type = "button";

    function finish(accepted) {
      backdrop.remove();
      if (!wasInert) appShell?.removeAttribute("inert");
      previousFocus?.focus?.();
      resolve(accepted);
    }

    if (cancel) cancel.onclick = () => finish(false);
    confirm.onclick = () => finish(true);
    backdrop.onclick = event => { if (event.target === backdrop) finish(false); };
    dialog.onkeydown = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      } else if (event.key === "Tab") {
        const first = cancel || confirm;
        const last = confirm;
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    (cancel || confirm).focus();
  });
}

function status(text, error = false) {
  byId("status").textContent = text;
  byId("status").className = error ? "status error" : "status";
}

function showSignedOut(broadcast = true) {
  if (!currentConfig?.authenticated && byId("appShell").classList.contains("signed-out")) return;
  const wasAuthenticated = currentConfig?.authenticated;
  authGeneration += 1;
  currentConfig = { ...currentConfig, authenticated: false };
  currentChatId = null;
  activeJobIds.clear();
  busy = false;
  fileSelectionBusy = false;
  chatSurfaceRequest += 1;
  clearInterval(processPollTimer);
  clearTimeout(sessionExpiryTimer);
  processPollTimer = null;
  sessionExpiryTimer = null;
  byId("chatList").replaceChildren();
  byId("workspaceContent").replaceChildren();
  byId("connectionDetail").replaceChildren();
  byId("activeProcesses").replaceChildren();
  byId("activeProcesses").hidden = true;
  byId("messageInput").value = "";
  byId("sendButton").disabled = false;
  byId("sendButton").textContent = "➤";
  document.querySelectorAll(".confirmation-dialog").forEach(dialog => {
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  byId("appShell").removeAttribute("inert");
  clearPendingFiles();
  accessPrompt(false);
  status(wasAuthenticated ? "Session ended. Sign in again." : "");
  if (wasAuthenticated && broadcast) authChannel?.postMessage("signed-out");
}

if (authChannel) {
  authChannel.onmessage = event => {
    if (event.data === "signed-out" && currentConfig?.authenticated) showSignedOut(false);
  };
}

async function refreshProcesses() {
  if (!currentConfig?.authenticated) return;
  const jobs = await api("/api/jobs");
  const panel = byId("activeProcesses");
  panel.replaceChildren();
  jobs.forEach(job => {
    add(panel, "div", `${job.chat_title}: ${job.stage} · ${job.elapsed_seconds}s elapsed`, "process-row");
  });
  panel.hidden = jobs.length === 0;
  const finishedCurrent = [...activeJobIds].some(id => !jobs.some(job => job.id === id));
  activeJobIds = new Set(jobs.map(job => job.id));
  if (finishedCurrent && currentChatId && !busy) await showChat(currentChatId);
}

function startProcessPolling() {
  if (processPollTimer) return;
  refreshProcesses().catch(error => status(error.message, true));
  processPollTimer = setInterval(() => {
    refreshProcesses().catch(error => status(error.message, true));
  }, 2000);
}

async function waitForJob(job) {
  while (true) {
    const state = await api(`/api/jobs/${job.id}`);
    await refreshProcesses();
    if (state.status !== "running") {
      if (currentChatId === job.chat_id) await showChat(job.chat_id);
      if (state.error) showError(state.error);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
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
  if (text) {
    if (role === "assistant") renderMarkdown(item, text);
    else add(item, "p", text, "assistant-text");
  }
  scrollBottom();
  return item;
}

function thinkingBubble() {
  const row = add(thread(), "div", "", "message-row assistant thinking-row");
  const item = add(row, "div", "", "bubble thinking-bubble");
  item.setAttribute("role", "status");
  item.setAttribute("aria-label", "Austra is working");
  const dots = add(item, "span", "", "thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) add(dots, "i");
  scrollBottom();
  return row;
}

function renderWelcome() {
  currentChatId = null;
  clearPendingFiles();
  byId("chatTitle").textContent = "New chat";
  byId("messages").replaceChildren();
  const welcome = add(byId("messages"), "div", "", "welcome");
  add(welcome, "div", "✦", "welcome-icon");
  add(welcome, "h1", "Start with a blank sheet");
  add(welcome, "p", "Describe your business in chat. Austra will ask for missing facts and propose skills, rules, and structures for your review.");
  thread();
}

function renderPendingFiles() {
  const preview = byId("attachmentPreview");
  preview.replaceChildren();
  preview.hidden = pendingFiles.length === 0;
  pendingFiles.forEach(({ file, digest }) => {
    const chip = add(preview, "div", "", "attachment-chip");
    add(chip, "span", `📎 ${file.name}`);
    add(chip, "small", `${(file.size / 1024).toFixed(0)} KB`);
    const remove = add(chip, "button", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.onclick = () => {
      pendingFiles = pendingFiles.filter(item => item.digest !== digest);
      renderPendingFiles();
    };
  });
}

function clearPendingFiles() {
  pendingFiles = [];
  byId("fileInput").value = "";
  renderPendingFiles();
}

function resizeMessageInput() {
  const input = byId("messageInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, parseFloat(getComputedStyle(input).maxHeight))}px`;
  input.style.overflowY = input.scrollHeight > input.clientHeight ? "auto" : "hidden";
}

function chatTitlePreview(title) {
  const characters = Array.from(title);
  return characters.length > CHAT_TITLE_PREVIEW_CHARS
    ? `${characters.slice(0, CHAT_TITLE_PREVIEW_CHARS).join("").trimEnd()}…`
    : title;
}

function closeChatMenus() {
  byId("chatList").querySelectorAll(".chat-menu").forEach(menu => {
    menu.hidden = true;
    menu.previousElementSibling.setAttribute("aria-expanded", "false");
  });
}

async function addPendingFiles(files) {
  if (busy || fileSelectionBusy) return;
  fileSelectionBusy = true;
  byId("sendButton").disabled = true;
  let duplicates = 0;
  try {
    for (const file of files) {
      if (pendingFiles.length >= MAX_ATTACHMENTS) {
        status(`Attach up to ${MAX_ATTACHMENTS} files at once.`, true);
        break;
      }
      if (!/\.(pdf|docx|csv|txt|md|xlsx)$/i.test(file.name)) {
        status("Attach a PDF, DOCX, CSV, TXT, MD, or XLSX file.", true);
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        status(`${file.name} exceeds 5 MB.`, true);
        continue;
      }
      const bytes = await file.arrayBuffer();
      const hash = crypto.subtle
        ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
            .map(value => value.toString(16).padStart(2, "0")).join("")
        : `${file.name}:${file.size}:${file.lastModified}`;
      if (pendingFiles.some(item => item.digest === hash)) {
        duplicates += 1;
        continue;
      }
      pendingFiles.push({ file, digest: hash });
    }
    renderPendingFiles();
    if (duplicates) status(`${duplicates} duplicate file${duplicates === 1 ? "" : "s"} skipped.`);
  } catch (error) {
    status(`Could not read the selected file: ${error.message}`, true);
  } finally {
    fileSelectionBusy = false;
    byId("sendButton").disabled = busy;
    byId("fileInput").value = "";
  }
}

async function refreshConfig() {
  const wasAuthenticated = currentConfig?.authenticated;
  const config = await api("/api/config");
  if (wasAuthenticated && !config.authenticated) {
    showSignedOut();
    return;
  }
  currentConfig = config;
  byId("appShell").classList.toggle("signed-out", !config.authenticated);
  clearTimeout(sessionExpiryTimer);
  if (config.authenticated) {
    sessionExpiryTimer = setTimeout(
      () => showSignedOut(), Math.ceil(config.session_seconds_remaining * 1000)
    );
  }
  byId("connection").textContent = currentConfig.connectors.anthropic
    ? "Austra model saved · checking" : "Austra model setup needed";
  byId("connection").classList.remove("ready");
}

function connectionStatus(connected) {
  byId("connection").textContent = connected ? "Austra model connected" : "Austra model connection failed";
  byId("connection").classList.toggle("ready", connected);
}

function showError(message) {
  status(message, true);
  showModal({ title: "Something went wrong", message });
}

async function testSavedModel() {
  try {
    await api("/api/connections/anthropic/test", { method: "POST" });
    connectionStatus(true);
    status("Austra model connection verified.");
  } catch (error) {
    if (!currentConfig?.authenticated) return;
    connectionStatus(false);
    status(error.message, true);
    securePrompt("anthropic");
  }
}

function securePrompt(connector) {
  if (!currentConfig.guides[connector]) return;
  const card = bubble(`${currentConfig.guides[connector].title} needs a connection.`);
  const button = add(card, "button", "Open Connections", "inline-button");
  button.type = "button";
  button.onclick = () => showConnections(connector);
}

function accessPrompt(setup) {
  byId("appShell").classList.add("signed-out");
  showChatView();
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
  const feedback = add(card, "p", "", "auth-feedback");
  feedback.setAttribute("role", "alert");
  button.onclick = async () => {
    try {
      await api(setup ? "/api/setup" : "/api/login", {
        method: "POST", body: JSON.stringify({ password: input.value })
      });
      input.value = "";
      await refreshConfig();
      if (!currentConfig.authenticated) return;
      renderWelcome();
      await loadChats();
      if (!currentConfig.authenticated) return;
      startProcessPolling();
      if (!currentConfig.connectors.anthropic) securePrompt("anthropic");
      else await testSavedModel();
      showGmailOauthResult();
    } catch (error) {
      feedback.textContent = error.message;
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
    const button = add(row, "button", chatTitlePreview(chat.title), `chat-link${chat.id === currentChatId ? " active" : ""}`);
    button.type = "button";
    button.title = chat.title;
    button.setAttribute("aria-label", chat.title);
    button.onclick = () => showChat(chat.id);
    const more = add(row, "button", "⋯", "chat-menu-trigger");
    more.type = "button";
    more.title = "Chat options";
    more.setAttribute("aria-label", `Options for ${chat.title}`);
    more.setAttribute("aria-expanded", "false");
    const menu = add(row, "div", "", "chat-menu");
    menu.hidden = true;
    more.onclick = event => {
      event.stopPropagation();
      const opening = menu.hidden;
      closeChatMenus();
      menu.hidden = !opening;
      more.setAttribute("aria-expanded", String(opening));
      if (opening) {
        menu.classList.toggle("above",
          row.getBoundingClientRect().bottom + menu.offsetHeight > list.getBoundingClientRect().bottom);
      }
    };
    const remove = add(menu, "button", "Delete chat", "chat-menu-delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete chat ${chat.title}`);
    remove.onclick = async () => {
      closeChatMenus();
      more.focus();
      if (!await showModal({
        title: "Delete chat?",
        message: `Remove “${chat.title}” from your chat list? An audit copy will remain.`,
        confirmLabel: "Delete chat",
        cancelLabel: "Cancel",
        destructive: true
      })) return;
      try {
        await api(`/api/chats/${chat.id}`, { method: "DELETE" });
        if (currentChatId === chat.id) renderWelcome();
        await loadChats();
        status("Chat removed from the list. Its audit copy remains.");
      } catch (error) { status(error.message, true); }
    };
  });
}

document.addEventListener("click", event => {
  if (!event.target.closest(".chat-list-item")) closeChatMenus();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeChatMenus();
});

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
  chat.messages.forEach(message => {
    const visual = message.role === "assistant" && (message.report || message.visualize);
    const showVisual = visual && Boolean(window.AustraA2UI);
    const item = bubble(
      visual && !showVisual ? message.body : message.display_body ?? message.body,
      message.role
    );
    item.dataset.messageId = message.id;
    if (visual) {
      item._austraFallback = message.body;
      if (showVisual) {
        const details = add(item, "details", "", "a2ui-source");
        add(details, "summary", "View full answer");
        renderMarkdown(details, message.body);
      }
    }
    (message.attachments || []).forEach(file => {
      const link = add(item, "a", `📎 ${file.name}`, "sent-attachment");
      link.href = `/api/chats/${chat.id}/attachments/${file.id}`;
      link.download = file.name;
    });
  });
  const surfaceRequest = ++chatSurfaceRequest;
  const reviewPanel = add(thread(), "div", "", "a2ui-panel");
  loadChatSurfaces(chat.id, reviewPanel, surfaceRequest, result.drafts || [])
    .catch(error => status(error.message, true));
  (result.credential_requests || []).forEach(name => {
    if (!currentConfig.connectors[name]) securePrompt(name);
  });
  if (result.analysis_error) showError(result.analysis_error);
  scrollBottom();
  loadChats().catch(error => status(error.message, true));
}

async function reviewDraft(draftId, decision) {
  await api(`/api/drafts/${draftId}/review`, {
    method: "POST", body: JSON.stringify({ decision })
  });
  status(`Draft ${decision}.`);
  if (!byId("workspacePage").hidden) await loadWorkspace();
  else if (currentChatId) await showChat(currentChatId);
}

async function loadChatSurfaces(chatId, panel, requestId, fallbackDrafts) {
  try {
    const response = await api(`/api/ui/chats/${chatId}`);
    if (requestId !== chatSurfaceRequest || currentChatId !== chatId) return;
    if (!window.AustraA2UI) throw new Error("A2UI renderer unavailable");
    response.surfaces.forEach(description => {
      const messageBubble = description.message_id
        ? [...thread().querySelectorAll(".bubble")].find(item => item.dataset.messageId === description.message_id)
        : null;
      const holder = add(messageBubble || panel, "div", "", "a2ui-item");
      window.AustraA2UI.mount(holder, description, async action => {
        try {
          if (description.kind === "draft") {
            const draftId = description.id.slice("draft_".length);
            const decisions = { approve_draft: "approved", reject_draft: "rejected" };
            if (decisions[action.name] && action.context?.draft_id === draftId) {
              await reviewDraft(draftId, decisions[action.name]);
            }
          } else if (description.kind === "answer" && action.name === "submit_answer") {
            const answer = String(action.context?.answer ?? "").trim();
            if (!answer) { status("Enter an answer first.", true); return; }
            byId("messageInput").value = answer;
            await sendMessage();
          }
        } catch (error) { status(error.message, true); }
      });
      if (description.kind === "report" && messageBubble) {
        const source = messageBubble.querySelector(".a2ui-source");
        if (description.visual_complete) source?.remove();
        else if (source) source.querySelector("summary").textContent = "View original text";
      }
      if (description.kind === "draft") {
        const details = add(holder, "details", "", "a2ui-source");
        add(details, "summary", "View full proposal");
        add(details, "pre", description.source, "skill-source");
      }
    });
    if (!panel.childElementCount) panel.remove();
    scrollBottom();
  } catch (error) {
    if (requestId !== chatSurfaceRequest || currentChatId !== chatId) return;
    panel.remove();
    thread().querySelectorAll(".bubble").forEach(item => {
      if (!item._austraFallback) return;
      item.replaceChildren();
      add(item, "div", "Austra", "assistant-label");
      renderMarkdown(item, item._austraFallback);
    });
    fallbackDrafts.filter(item => !["schema", "record"].includes(item.kind)).forEach(renderDraft);
  }
}

async function showChat(chatId) {
  try {
    if (chatId !== currentChatId) clearPendingFiles();
    showChatView();
    renderChat(await api(`/api/chats/${chatId}`));
    byId("sidebar").classList.remove("open");
  } catch (error) { status(error.message, true); }
}

async function sendMessage() {
  if (busy || fileSelectionBusy || !currentConfig?.authenticated) return;
  const input = byId("messageInput");
  const text = input.value.trim();
  const selectedFiles = [...pendingFiles];
  if (!text && !selectedFiles.length) return;
  if (/sk-ant-|xox[baprs]-|-----BEGIN .*PRIVATE KEY-----/i.test(text)) {
    status("Use Connections to enter credentials privately.", true);
    return;
  }
  busy = true;
  const requestGeneration = authGeneration;
  byId("sendButton").disabled = true;
  byId("sendButton").textContent = "…";
  byId("messages").setAttribute("aria-busy", "true");
  status("");
  input.value = "";
  resizeMessageInput();
  clearPendingFiles();
  const optimistic = bubble(text, "user");
  selectedFiles.forEach(({ file }) => add(optimistic, "div", `📎 ${file.name}`, "sent-attachment"));
  const thinking = thinkingBubble();
  scrollBottom();
  try {
    const form = new FormData();
    form.append("body", text);
    if (currentChatId) form.append("chat_id", currentChatId);
    selectedFiles.forEach(({ file }) => form.append("uploads", file, file.name));
    const response = await fetch("/api/messages/background", {
      method: "POST", credentials: "same-origin", body: form
    });
    const result = await response.json();
    if (requestGeneration !== authGeneration) return;
    if (response.status === 401) {
      showSignedOut();
      return;
    }
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Send failed");
    renderChat(result);
    if (result.job) {
      activeJobIds.add(result.job.id);
      await refreshProcesses();
      waitForJob(result.job).catch(error => {
        if (currentConfig?.authenticated) showError(error.message);
      });
    }
    if (result.duplicate_files?.length) {
      status(`${result.duplicate_files.length} duplicate file${result.duplicate_files.length === 1 ? "" : "s"} skipped.`);
    } else if (!result.analysis_error) status("");
  } catch (error) {
    if (requestGeneration !== authGeneration) return;
    thinking.remove();
    optimistic.parentElement.remove();
    input.value = text;
    resizeMessageInput();
    pendingFiles = selectedFiles;
    renderPendingFiles();
    showError(error.message);
  } finally {
    busy = false;
    byId("sendButton").disabled = false;
    byId("sendButton").textContent = "➤";
    byId("messages").removeAttribute("aria-busy");
    if (currentConfig?.authenticated) input.focus();
  }
}

function showChatView() {
  byId("connectionsPage").hidden = true;
  byId("workspacePage").hidden = true;
  byId("chatArea").hidden = false;
  byId("sidebar").classList.remove("open");
  scrollBottom();
}

function connectionFeedback(text, error = false, loading = false) {
  const feedback = byId("connectionFeedback");
  feedback.textContent = text;
  feedback.className = `connection-feedback${error ? " error" : ""}${loading ? " loading" : ""}`;
}

function renderConnectionList() {
  const list = byId("connectionList");
  list.replaceChildren();
  Object.entries(currentConfig.guides).forEach(([name, guide]) => {
    const button = add(list, "button", "", `connection-option${name === selectedConnector ? " active" : ""}`);
    button.type = "button";
    add(button, "strong", guide.title);
    add(button, "small", currentConfig.connectors[name] ? "Saved" : "Setup needed");
    button.onclick = () => { selectedConnector = name; renderConnectionList(); renderConnectionDetail(); };
  });
  const mcpButton = add(list, "button", "", `connection-option${selectedConnector === "mcp" ? " active" : ""}`);
  mcpButton.type = "button";
  add(mcpButton, "strong", "Other apps via MCP");
  add(mcpButton, "small", "Add a reusable server");
  mcpButton.onclick = () => { selectedConnector = "mcp"; renderConnectionList(); renderConnectionDetail(); };
}

async function testConnection(connector) {
  const button = byId("testConnection");
  button.disabled = true;
  button.textContent = "Testing…";
  connectionFeedback("Testing connection…", false, true);
  try {
    await api(`/api/connections/${connector}/test`, { method: "POST" });
    if (connector === "anthropic") connectionStatus(true);
    if (connector === "slack") {
      await refreshConfig();
      renderConnectionList();
      renderConnectionDetail();
      connectionFeedback(currentConfig.slack_socket_connected
        ? "Slack credentials verified. Socket Mode is connected and ready for messages."
        : "Slack credentials verified. Enable Socket Mode and wait for Austra to connect, then refresh status.");
    } else {
      connectionFeedback("Connection verified successfully.");
    }
  } catch (error) {
    if (connector === "anthropic") connectionStatus(false);
    connectionFeedback(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Test connection";
  }
}

async function beginGmailConnect() {
  const button = byId("connectGmail");
  button.disabled = true;
  button.textContent = "Opening Google…";
  connectionFeedback("Opening Google authorization…", false, true);
  try {
    const result = await api("/api/connections/gmail/start", { method: "POST" });
    window.location.assign(result.authorization_url);
  } catch (error) {
    connectionFeedback(error.message, true);
    button.disabled = false;
    button.textContent = "Connect with Google";
  }
}

async function renderMcpDetail() {
  const detail = byId("connectionDetail");
  detail.replaceChildren();
  add(detail, "span", "MCP CONNECTIONS", "eyebrow");
  add(detail, "h2", "Connect another app");
  add(detail, "p", "Add a Streamable HTTP MCP server. Austra discovers its tools and keeps the access token private.", "connection-description");
  const form = add(detail, "form", "", "connection-form");
  const fields = [
    ["name", "Connection name", "Example: teams"],
    ["url", "MCP server URL", "https://example.com/mcp"],
    ["token", "Bearer token (if required)", "Saved privately"],
  ];
  const inputs = {};
  fields.forEach(([name, title, placeholder]) => {
    const label = add(form, "label", title);
    const input = add(label, "input");
    input.type = name === "token" ? "password" : "text";
    input.placeholder = placeholder;
    input.autocomplete = "off";
    input.required = name !== "token";
    inputs[name] = input;
  });
  const save = add(form, "button", "Connect MCP server", "primary");
  save.type = "submit";
  const feedback = add(detail, "div", "", "connection-feedback");
  feedback.id = "connectionFeedback";
  feedback.setAttribute("role", "status");
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true;
    connectionFeedback("Checking the server and discovering tools…", false, true);
    try {
      const payload = Object.fromEntries(fields.map(([name]) => [name, inputs[name].value.trim()]));
      await api("/api/mcp/connections", { method: "POST", body: JSON.stringify(payload) });
      await renderMcpDetail();
      connectionFeedback(`${payload.name} connected. Its tools are ready.`);
    } catch (error) {
      connectionFeedback(error.message, true);
      save.disabled = false;
    }
  };
  try {
    const connections = await api("/api/mcp/connections");
    Object.entries(connections).forEach(([name, connection]) => {
      const row = add(detail, "div", "", "connection-form");
      add(row, "strong", name.replaceAll("_", " "));
      add(row, "small", connection.url);
      if (connection.tools.length) {
        add(row, "small", `${connection.tools.length} tools: ${connection.tools.map(tool => tool.name).join(", ")}`);
      }
      const test = add(row, "button", "Discover tools", "top-action");
      test.type = "button";
      test.onclick = async () => {
        try {
          const result = await api(`/api/mcp/connections/${name}/test`, { method: "POST" });
          connectionFeedback(`${name}: ${result.tools.length} tools discovered.`);
          if (!connection.built_in) await renderMcpDetail();
        } catch (error) { connectionFeedback(error.message, true); }
      };
      if (!connection.built_in) {
        const remove = add(row, "button", "Remove", "top-action");
        remove.type = "button";
        remove.onclick = async () => {
          try {
            await api(`/api/mcp/connections/${name}`, { method: "DELETE" });
            await renderMcpDetail();
            connectionFeedback(`${name} removed.`);
          } catch (error) { connectionFeedback(error.message, true); }
        };
      }
    });
  } catch (error) { connectionFeedback(error.message, true); }
}

function renderConnectionDetail() {
  if (selectedConnector === "mcp") { renderMcpDetail(); return; }
  const connector = selectedConnector;
  const guide = currentConfig.guides[connector];
  const detail = byId("connectionDetail");
  detail.replaceChildren();
  add(detail, "span", currentConfig.connectors[connector] ? "CREDENTIALS SAVED" : "SETUP NEEDED", "eyebrow");
  add(detail, "h2", guide.title);
  add(detail, "p", guide.steps, "connection-description");
  const link = add(detail, "a", "Open official setup guide ↗", "guide-link");
  link.href = guide.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  if (connector === "gmail") {
    add(detail, "div", "Authorized redirect URI", "redirect-label");
    add(detail, "code", currentConfig.gmail_redirect_uri, "redirect-uri");
  }
  if (connector === "slack") {
    add(detail, "p", currentConfig.slack_socket_connected
      ? "Socket Mode connected · Austra can receive Slack events"
      : "Socket Mode disconnected · enable it in Slack and save an app-level token",
    "connection-description");
    const refresh = add(detail, "button", "Refresh Socket Mode status", "top-action");
    refresh.type = "button";
    refresh.onclick = async () => {
      await refreshConfig();
      renderConnectionDetail();
    };
  }
  const form = add(detail, "form", "", "connection-form");
  const inputs = new Map();
  const fields = currentConfig.fields?.[connector];
  if (!Array.isArray(fields)) {
    add(detail, "p", "Connection settings are out of date. Reload Austra to continue.", "connection-feedback error");
    const reload = add(detail, "button", "Reload Austra", "top-action");
    reload.type = "button";
    reload.onclick = () => window.location.reload();
    return;
  }
  fields.forEach(name => {
    const label = add(form, "label", name.replaceAll("_", " "));
    const input = add(label, "input");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = currentConfig.missing[connector].includes(name)
      ? (name === "SLACK_ALLOWED_TEAM_ID" ? "Workspace ID starting with T" : "Enter value")
      : "Saved privately · enter to replace";
    input.setAttribute("aria-label", name);
    if (name === "SLACK_ALLOWED_TEAM_ID") {
      add(label, "small", "Use the workspace ID starting with T, not an xapp- token.");
    }
    if (name === "SLACK_APP_TOKEN") {
      add(label, "small", "App-level token with connections:write, starting with xapp-.");
    }
    inputs.set(name, input);
  });
  const actions = add(form, "div", "", "connection-actions");
  const save = add(actions, "button", "Save credentials", "primary");
  save.type = "submit";
  if (connector === "gmail") {
    save.textContent = "Save OAuth client";
    const connect = add(actions, "button", "Connect with Google", "top-action");
    connect.type = "button";
    connect.id = "connectGmail";
    const oauthReady = !currentConfig.missing.gmail.includes("GMAIL_CLIENT_ID")
      && !currentConfig.missing.gmail.includes("GMAIL_CLIENT_SECRET");
    connect.disabled = !oauthReady;
    if (!oauthReady) connect.title = "Save the OAuth client details first";
    connect.onclick = beginGmailConnect;
  }
  const test = add(actions, "button", "Test connection", "top-action");
  test.type = "button";
  test.id = "testConnection";
  if (connector === "gmail" && !currentConfig.connectors.gmail) {
    test.disabled = true;
    test.title = "Connect with Google first";
  }
  test.onclick = () => testConnection(connector);
  const feedback = add(detail, "div", "", "connection-feedback");
  feedback.id = "connectionFeedback";
  feedback.setAttribute("role", "status");
  form.onsubmit = async event => {
    event.preventDefault();
    const values = Object.fromEntries([...inputs].filter(([, input]) => input.value.trim())
      .map(([name, input]) => [name, input.value.trim()]));
    if (!Object.keys(values).length) {
      connectionFeedback("Enter a value to save or use Test connection.", true);
      return;
    }
    save.disabled = true;
    save.textContent = "Saving…";
    connectionFeedback("Saving credentials privately…", false, true);
    try {
      await api(`/api/credentials/${connector}`, { method: "POST", body: JSON.stringify({ values }) });
      await refreshConfig();
      renderConnectionList();
      renderConnectionDetail();
      connectionFeedback(currentConfig.connectors[connector]
        ? "Credentials saved. Testing connection…"
        : (connector === "gmail" ? "OAuth client saved. Choose Connect with Google." : "Saved. Complete the remaining fields to test."));
      if (currentConfig.connectors[connector]) await testConnection(connector);
    } catch (error) {
      connectionFeedback(error.message, true);
      save.disabled = false;
      save.textContent = "Save credentials";
    }
  };
}

function showGmailOauthResult() {
  const query = new URLSearchParams(window.location.search);
  if (query.get("connection") !== "gmail" || !query.has("oauth")) return;
  showConnections("gmail");
  connectionFeedback(query.get("oauth") === "connected"
    ? "Gmail connected and mailbox verified."
    : "Gmail authorization was not completed. Choose Connect with Google to try again.",
  query.get("oauth") !== "connected");
  window.history.replaceState(null, "", "/");
}

function showConnections(connector = "anthropic") {
  if (!currentConfig?.authenticated) return;
  selectedConnector = connector === "mcp" || currentConfig.guides[connector] ? connector : "anthropic";
  byId("chatArea").hidden = true;
  byId("workspacePage").hidden = true;
  byId("connectionsPage").hidden = false;
  byId("sidebar").classList.remove("open");
  renderConnectionList();
  renderConnectionDetail();
}

function renderWorkspaceTabs() {
  const tabs = byId("workspaceTabs");
  tabs.replaceChildren();
  [
    ["records", "Records"], ["files", "Approved files"],
    ["drafts", "To review"], ["activity", "Activity"]
  ].forEach(([section, label]) => {
    const button = add(tabs, "button", label, section === workspaceSection ? "active" : "");
    button.type = "button";
    button.setAttribute("aria-current", section === workspaceSection ? "page" : "false");
    button.onclick = () => {
      workspaceSection = section;
      workspaceOffset = 0;
      renderWorkspaceTabs();
      loadWorkspace().catch(error => status(error.message, true));
    };
  });
}

async function loadWorkspace() {
  const section = workspaceSection;
  const offset = workspaceOffset;
  const content = byId("workspaceContent");
  content.replaceChildren();
  add(content, "p", "Loading workspace…", "workspace-loading");
  const response = await api(`/api/ui/workspace?section=${section}&offset=${offset}`);
  if (section !== workspaceSection || offset !== workspaceOffset || byId("workspacePage").hidden) return;
  content.replaceChildren();
  if (!window.AustraA2UI) { add(content, "p", "A2UI renderer unavailable."); return; }
  const pageItemIds = new Set(response.items.map(item => item.id).filter(Boolean));
  window.AustraA2UI.mount(content, response.surface, async action => {
    const decisions = { approve_draft: "approved", reject_draft: "rejected" };
    const draftId = action.context?.draft_id;
    if (!decisions[action.name] || !pageItemIds.has(draftId)) return;
    try { await reviewDraft(draftId, decisions[action.name]); }
    catch (error) { status(error.message, true); }
  });
  response.items.forEach(item => {
    const details = add(content, "details", "", "a2ui-source");
    add(details, "summary", `View full source · ${item.title}`);
    add(details, "pre", item.source, "skill-source");
  });
  const pagination = byId("workspacePagination");
  pagination.replaceChildren();
  const shown = Math.min(response.total, offset + response.page_size);
  add(pagination, "span", response.total ? `${offset + 1}–${shown} of ${response.total}` : "0 items");
  const previous = add(pagination, "button", "Previous");
  previous.type = "button";
  previous.disabled = offset === 0;
  previous.onclick = () => { workspaceOffset = Math.max(0, offset - response.page_size); loadWorkspace(); };
  const next = add(pagination, "button", "Next");
  next.type = "button";
  next.disabled = shown >= response.total;
  next.onclick = () => { workspaceOffset = offset + response.page_size; loadWorkspace(); };
}

function showWorkspace() {
  if (!currentConfig?.authenticated) return;
  byId("chatArea").hidden = true;
  byId("connectionsPage").hidden = true;
  byId("workspacePage").hidden = false;
  byId("sidebar").classList.remove("open");
  renderWorkspaceTabs();
  loadWorkspace().catch(error => status(error.message, true));
}

async function showInfrastructure() {
  try {
    const result = await api("/api/infrastructure");
    const lines = Object.entries(result.services).map(([name, state]) => `${name}: ${state}`);
    bubble(`Optional Compose services\n${lines.join("\n")}\n\nAsk Austra to propose a start or stop change. Approve its infrastructure draft to apply it.`);
  } catch (error) { status(error.message, true); }
}

byId("newChat").onclick = () => { if (currentConfig?.authenticated) { showChatView(); renderWelcome(); } };
byId("sendButton").onclick = sendMessage;
byId("messageInput").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
};
byId("messageInput").oninput = resizeMessageInput;
byId("attachButton").onclick = () => byId("fileInput").click();
byId("fileInput").onchange = event => addPendingFiles([...event.target.files]);
byId("connectionsButton").onclick = () => showConnections();
byId("workspaceButton").onclick = showWorkspace;
byId("workspaceBack").onclick = showChatView;
byId("topConnections").onclick = () => showConnections();
byId("backToChat").onclick = showChatView;
byId("infrastructureButton").onclick = showInfrastructure;
byId("menuButton").onclick = () => byId("sidebar").classList.toggle("open");
byId("logoutButton").onclick = async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showSignedOut();
  } catch (error) { status(error.message, true); }
};

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentConfig?.authenticated) {
    refreshConfig().catch(error => status(error.message, true));
  }
});

async function start() {
  try {
    await refreshConfig();
    if (!currentConfig.admin_ready) accessPrompt(true);
    else if (!currentConfig.authenticated) accessPrompt(false);
    else {
      renderWelcome();
      await loadChats();
      if (!currentConfig.authenticated) return;
      startProcessPolling();
      if (!currentConfig.connectors.anthropic) securePrompt("anthropic");
      else await testSavedModel();
      showGmailOauthResult();
    }
  } catch (error) { status(error.message, true); }
}

start();
