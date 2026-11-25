let currentChatId = null;
let workflowConversation = null;
let stopWorkflowAlerts = null;
let currentConfig = null;
let modelConnectionVerified = false;
let tourState = null;
let organizationChatMode = "discovery";
let askDepartmentId = "";
let tourStarting = false;
let pendingFiles = [];
let fileSelectionBusy = false;
let busy = false;
let activeJobIds = new Set();
let activeStreamJobs = new Map();
let processPollTimer = null;
let sessionExpiryTimer = null;
let authGeneration = 0;
const authChannel = typeof window.BroadcastChannel === "function"
  ? new window.BroadcastChannel("austra-auth") : null;
let selectedConnector = "anthropic";
let workspaceSection = "records";
let workspaceOffset = 0;
let workspaceDepartmentId = "";
let workspaceRequest = 0;
let infrastructureRequest = 0;
let chatSurfaceRequest = 0;
let chatOpenRequest = 0;
const ORGANIZATION_PAGES = new Set([
  "/organization/departments", "/organization/people", "/organization/workflows",
  "/organization/workflow-reviews", "/organization/onboarding", "/organization/capability",
  "/organization/connections", "/organization/monitoring", "/organization/info",
  "/organization/workflow-planning"
]);
const WORKSPACE_SECTIONS = {
  records: { label: "Records", empty: "No records yet", description: "Saved records will appear here." },
  files: { label: "Approved files", empty: "No approved files yet", description: "Files appear here after approval." },
  drafts: { label: "To review", empty: "Nothing to review", description: "New proposals will appear here." },
  activity: { label: "Activity", empty: "No activity yet", description: "Workspace changes will appear here." }
};
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
const MAX_IMAGE_REQUEST_BYTES = 18 * 1024 * 1024;

const byId = id => document.getElementById(id);

async function api(path, options = {}) {
  const requestGeneration = authGeneration;
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-Austra-Request": "1", ...(options.headers || {}) }
  });
  const data = await response.json().catch(() => null);
  if (requestGeneration !== authGeneration) throw new Error("Your session ended. Sign in again.");
  if (response.status === 401 && path !== "/api/login" && path !== "/api/setup") {
    showSignedOut();
    throw new Error("Your session ended. Sign in again.");
  }
  if (!response.ok) {
    const invalid = response.status === 422 && Array.isArray(data?.detail) && data.detail[0];
    const hint = invalid?.loc?.at(-1)?.toString().replaceAll("_", " ");
    throw new Error(typeof data?.detail === "string" ? data.detail
      : hint ? `Check ${hint}. ${invalid.msg}` : `Request failed (HTTP ${response.status})`);
  }
  if (data === null) throw new Error("The server returned an unreadable response. Please retry.");
  return data;
}

async function postStream(path, payload, onEvent) {
  const requestGeneration = authGeneration;
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Austra-Request": "1" },
    body: JSON.stringify(payload)
  });
  if (response.status === 401) {
    showSignedOut();
    throw new Error("Your session ended. Sign in again.");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string"
      ? error.detail : `Request failed (HTTP ${response.status})`);
  }
  if (!response.body?.getReader) throw new Error("Streaming is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    if (requestGeneration !== authGeneration) throw new Error("Your session ended. Sign in again.");
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const event = frame.match(/^event: (.+)$/m)?.[1];
      const data = frame.match(/^data: (.+)$/m)?.[1];
      if (!event || !data) continue;
      const parsed = JSON.parse(data);
      if (event === "error") throw new Error(parsed.detail);
      if (event === "done") result = parsed;
      onEvent(event, parsed);
    }
    if (done) break;
  }
  if (!result) throw new Error("The answer stream ended early. Please retry.");
  return result;
}

function add(parent, tag, text = "", className = "") {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  parent.appendChild(item);
  return item;
}

function showLoadingState(target, label, variant = "table") {
  target.replaceChildren();
  const state = add(target, "div", "", `loading-state loading-${variant}`);
  state.setAttribute("role", "status");
  state.setAttribute("aria-label", label);
  state.setAttribute("aria-busy", "true");
  const shape = add(state, "div", "", "loading-shape");
  shape.setAttribute("aria-hidden", "true");
  add(shape, "div", "", "loading-bar loading-title");
  add(shape, "div", "", "loading-bar loading-subtitle");
  const cards = add(shape, "div", "", "loading-cards");
  for (let index = 0; index < 3; index += 1) add(cards, "div", "", "loading-bar loading-card");
  const table = add(shape, "div", "", "loading-table");
  for (let index = 0; index < 4; index += 1) add(table, "div", "", "loading-bar loading-row");
  return state;
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
  stopWorkflowAlerts?.();
  stopWorkflowAlerts = null;
  activeStreamJobs.forEach(stream => stream.close());
  activeStreamJobs.clear();
  if (!currentConfig?.authenticated && byId("appShell").classList.contains("signed-out")) return;
  const wasAuthenticated = currentConfig?.authenticated;
  byId("platformRoot").hidden = true;
  byId("appShell").hidden = false;
  document.querySelectorAll(".platform-backdrop").forEach(backdrop => backdrop.remove());
  byId("platformRoot").replaceChildren();
  authGeneration += 1;
  currentConfig = { ...currentConfig, authenticated: false };
  modelConnectionVerified = false;
  window.clearOrganizationManagementCache?.();
  currentChatId = null;
  tourState = null;
  tourStarting = false;
  activeJobIds.clear();
  busy = false;
  fileSelectionBusy = false;
  chatSurfaceRequest += 1;
  clearInterval(processPollTimer);
  clearTimeout(sessionExpiryTimer);
  processPollTimer = null;
  sessionExpiryTimer = null;
  byId("chatList").replaceChildren();
  byId("sidebarPages").open = false;
  byId("workspaceContent").replaceChildren();
  byId("workspacePagination").replaceChildren();
  byId("workspacePagination").hidden = true;
  byId("infrastructureContent").replaceChildren();
  byId("connectionDetail").replaceChildren();
  byId("accountContent").replaceChildren();
  byId("appShell").classList.remove("tour-mode");
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
  const finishedCurrent = [...activeJobIds].some(id =>
    !jobs.some(job => job.id === id) && !activeStreamJobs.has(id));
  activeJobIds = new Set(jobs.map(job => job.id));
  if (finishedCurrent && currentChatId && !busy) await showChat(currentChatId, false);
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
      if (currentChatId === job.chat_id) await showChat(job.chat_id, false);
      if (state.error) showError(state.error);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
}

function streamJob(job) {
  if (typeof window.EventSource !== "function") return waitForJob(job);
  const source = new window.EventSource(`/api/jobs/${job.id}/events`);
  activeStreamJobs.set(job.id, source);
  let pending = "";
  let frame = null;
  let item = null;
  const flush = () => {
    frame = null;
    if (currentChatId !== job.chat_id || !pending) return;
    if (!item || !item.isConnected) {
      item = bubble("", "assistant");
      item.parentElement.classList.add("streaming-row");
      item.querySelector(".assistant-label").textContent = "Austra · drafting";
      add(item, "p", "", "streaming-text");
    }
    item.querySelector(".streaming-text").textContent += pending;
    pending = "";
    scrollBottom();
  };
  source.addEventListener("stage", event => {
    if (currentChatId === job.chat_id) status(JSON.parse(event.data).stage);
  });
  source.addEventListener("delta", event => {
    pending += JSON.parse(event.data).text;
    if (frame === null) frame = window.requestAnimationFrame(flush);
  });
  source.addEventListener("done", event => {
    source.close();
    activeStreamJobs.delete(job.id);
    if (frame !== null) window.cancelAnimationFrame(frame);
    const result = JSON.parse(event.data);
    if (currentChatId === job.chat_id) {
      showChat(job.chat_id, false).then(() => {
        if (result.error) showError(result.error);
        else status("");
      });
    }
    refreshProcesses().catch(error => status(error.message, true));
  });
  source.onerror = () => {
    source.close();
    activeStreamJobs.delete(job.id);
    if (frame !== null) window.cancelAnimationFrame(frame);
    waitForJob(job).catch(error => {
      if (currentConfig?.authenticated) showError(error.message);
    });
  };
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
  add(item, "span", "Thinking", "thinking-label");
  const dots = add(item, "span", "", "thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) add(dots, "i");
  scrollBottom();
  return row;
}

function setThinkingStage(row, stage) {
  if (!row || typeof stage !== "string" || !stage.trim()) return;
  row.querySelector(".thinking-label").textContent = stage;
  row.querySelector(".thinking-bubble").setAttribute("aria-label", `Austra: ${stage}`);
  scrollBottom();
}

function renderWelcome() {
  workflowConversation = null;
  chatOpenRequest += 1;
  currentChatId = null;
  clearPendingFiles();
  const organizationWorkspace = currentConfig?.legacy_workspace === false;
  if (organizationWorkspace) organizationChatMode = "knowledge";
  byId("chatTitle").textContent = "New chat";
  syncChatSelection();
  byId("messages").replaceChildren();
  const welcome = add(byId("messages"), "div", "", "welcome");
  add(welcome, "h1", "How can Austra help?");
  if (currentConfig?.saas_mode && currentConfig.authenticated) {
    const plan = add(welcome, "button", "Plan a workflow with Austra", "org-button secondary");
    plan.onclick = () => navigateAustra("/organization/workflow-planning").catch(error => status(error.message, true));
  }
  if (organizationWorkspace && (tourState?.completed || currentConfig.role === "employee")) {
    const noAccess = employeeHasNoChatDepartment();
    add(welcome, "p", noAccess
      ? "You don’t have departmental chat access yet. Ask your organization admin for access."
      : "Ask a question about approved business information.");
    if (currentConfig.role === "org_admin") {
      const actions = add(welcome, "div", "", "suggestions");
      const plan = add(actions, "button", "Plan a process");
      plan.type = "button";
      plan.onclick = renderOnboardingChat;
    }
    byId("messageInput").placeholder = "Ask about approved information…";
    thread();
    return;
  }
  add(welcome, "p", organizationWorkspace
    ? "Ask about approved information or describe a process you want to improve."
    : "Ask a question about saved records or describe a change for Austra to draft.");
  byId("messageInput").placeholder = "Message Austra…";
  if (!organizationWorkspace && currentConfig?.authenticated && canManageConnections()) {
    const actions = add(welcome, "div", "", "suggestions");
    const browse = add(actions, "button", "Browse workspace");
    browse.type = "button";
    browse.onclick = () => navigateAustra("/workspace").catch(error => status(error.message, true));
    const draft = add(actions, "button", "Describe a change");
    draft.type = "button";
    draft.onclick = () => {
      const input = byId("messageInput");
      if (!input.value) input.value = "I’d like to change ";
      resizeMessageInput();
      input.focus();
    };
  }
  thread();
}

function renderOnboardingChat(selectedWorkflow) {
  if (!tourState?.completed || currentConfig?.role !== "org_admin") return;
  chatOpenRequest += 1;
  currentChatId = null;
  organizationChatMode = "discovery";
  clearPendingFiles();
  showChatView();
  byId("chatTitle").textContent = `${tourState.organization_name} onboarding`;
  syncChatSelection();
  byId("messageInput").placeholder = "Describe a process…";
  byId("messages").replaceChildren();
  thread();
  tourState.messages.forEach(renderDiscoveryMessage);
  if (tourState.workflows?.length) {
    const label = add(thread(), "label", "Workflow ");
    const selector = add(label, "select");
    selector.id = "discoveryWorkflow";
    add(selector, "option", "Continue conversation").value = "";
    add(selector, "option", "Start a separate workflow").value = "new";
    for (const workflow of tourState.workflows.filter(item => item.state !== "locked_candidate")) {
      add(selector, "option", workflow.name || "Untitled workflow").value = workflow.id;
    }
    if (typeof selectedWorkflow === "string" && [...selector.options].some(
      option => option.value === selectedWorkflow
    )) selector.value = selectedWorkflow;
  }
  scrollBottom();
}

function renderDiscoveryMessage(message) {
  const item = bubble(message.body, message.role);
  if (message.role === "assistant" && message.workspace_consent) {
    mountWorkspaceConsent(item, message.workspace_consent);
  }
}

function mountWorkspaceConsent(message, description) {
  if (!window.AustraA2UI || currentConfig?.role !== "org_admin") return;
  const holder = add(message, "div", "", "a2ui-item");
  let pending = false;
  const unmount = window.AustraA2UI.mount(holder, description, async action => {
    if (pending || busy || !holder.isConnected) return;
    const connect = action.name === "connect_google_workspace";
    if (!connect && action.name !== "check_google_workspace") return;
    pending = true;
    holder.setAttribute("aria-busy", "true");
    const generation = authGeneration;
    let popup;
    try {
      if (!connect) {
        const result = await api("/api/org/connections/google-workspace/status");
        const app = result.apps.find(item => item.name === description.app
          && ["healthy", "schema_review"].includes(item.health));
        feedback.textContent = app
          ? `Google consent is saved for ${app.name}. Check Connected apps for tool readiness and approvals.`
          : `Access to ${description.app} is still pending. Connect or finish consent in the Google tab.`;
        if (app) unmount();
        return;
      }
      popup = window.open("about:blank", "_blank");
      if (!popup) {
        feedback.textContent = "Allow Austra to open the Google sign-in tab, then try again.";
        return;
      }
      popup.opener = null;
      feedback.textContent = "Opening Google sign-in…";
      const result = await api("/api/org/connections/google-workspace/start", {
        method: "POST", body: JSON.stringify({ workflow_id: description.workflow_id ?? null })
      });
      if (generation !== authGeneration) { popup.close(); return; }
      const target = new URL(result.authorization_url);
      if (target.origin !== "https://accounts.google.com" || target.pathname !== "/o/oauth2/v2/auth") {
        throw new Error("Google sign-in could not be opened.");
      }
      popup.location.replace(target.href);
      feedback.textContent = "Finish Google consent in the new tab. Your workflow conversation stays open here.";
    } catch (error) {
      popup?.close();
      feedback.textContent = error.message;
    } finally {
      pending = false;
      holder.removeAttribute("aria-busy");
    }
  });
  const feedback = add(holder, "p", "", "workspace-consent-status");
  feedback.setAttribute("role", "status");
}

function showWorkspaceOauthResult() {
  const query = new URLSearchParams(window.location.search);
  const result = query.get("workspace_oauth");
  if (!result || currentConfig?.role !== "org_admin") return;
  if (currentConfig.legacy_workspace === false && tourState?.completed) {
    renderOnboardingChat(query.get("workflow"));
  }
  window.history.replaceState(null, "", "/");
  const messages = {
    connected: "Google consent saved. Continue the workflow here. External actions still require review and approval.",
    partial: "Google consent saved. Some Workspace apps need repair in Connected apps before their actions can run.",
    denied: "Google consent was declined. You can continue the draft or connect again.",
    expired: "Google consent expired. Connect again from this draft.",
    file_scope_missing: "Google did not grant file access. Connect again and select the Google Drive file-access permission in the consent screen.",
    client_rejected: "Google rejected the OAuth client. Ask your platform admin to check its registration.",
    grant_rejected: "Google rejected the consent grant. Connect again to start a fresh authorization.",
    offline_missing: "Google did not allow Austra to refresh its access. Connect again to grant access for this workflow.",
    account_unverified: "Google did not verify your account. Use the test Google account and allow the requested account details.",
    catalog_unavailable: "Google granted access, but a Workspace tool catalog could not be reached. Check that the Workspace MCP APIs are enabled in Google Cloud.",
    store_unavailable: "Google granted access, but Austra could not save the connection. Try connecting again.",
    failed: "Austra could not verify Google Workspace access. Check the Google account and try again."
  };
  status(messages[result] || messages.failed, !["connected", "partial"].includes(result));
}

function tourBubble(message) {
  const item = bubble(message);
  item.parentElement.classList.add("tour-message");
  return item;
}

function renderTour() {
  if (!tourState || tourState.completed || !currentConfig?.authenticated) return;
  showChatView();
  byId("appShell").classList.add("tour-mode");
  byId("chatTitle").textContent = "Welcome to Austra";
  byId("messages").replaceChildren();
  byId("messageInput").placeholder = "Reply to Austra…";
  byId("messageInput").disabled = !currentConfig.connectors.anthropic || tourStarting || busy;
  byId("sendButton").disabled = byId("messageInput").disabled || busy;
  byId("attachButton").disabled = busy;
  thread();
  if (!currentConfig.connectors.anthropic) {
    const card = tourBubble("👋 Add your Anthropic API key to begin. I’ll verify it securely, then we can talk about your organization.");
    const input = add(card, "input", "", "secure-input");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = "Anthropic API key";
    input.setAttribute("aria-label", "Anthropic API key");
    const button = add(card, "button", "Check key & continue →", "inline-button");
    button.type = "button";
    const feedback = add(card, "p", "", "auth-feedback");
    feedback.setAttribute("role", "alert");
    button.onclick = async () => {
      button.disabled = true;
      feedback.textContent = "Checking your key…";
      try {
        await api("/api/credentials/anthropic", { method: "POST", body: JSON.stringify({ values: { ANTHROPIC_API_KEY: input.value.trim() } }) });
        input.value = "";
        await refreshConfig();
        connectionStatus(true);
        renderTour();
      } catch (error) {
        feedback.textContent = error.message;
        button.disabled = false;
      }
    };
    input.onkeydown = event => { if (event.key === "Enter") button.click(); };
    input.focus();
    return;
  }
  if (!tourState.messages?.length) {
    thinkingBubble();
    if (!tourStarting) startTour();
    return;
  }
  tourState.messages.forEach(message => {
    if (message.role === "assistant") tourBubble(message.body);
    else bubble(message.body, "user");
  });
  scrollBottom();
  byId("messageInput").focus();
}

async function startTour() {
  tourStarting = true;
  byId("messageInput").disabled = true;
  byId("sendButton").disabled = true;
  const requestGeneration = authGeneration;
  try {
    const result = await api("/api/org/tour/start", { method: "POST" });
    if (requestGeneration !== authGeneration) return;
    if (!Array.isArray(result.messages) || !result.messages.length) {
      throw new Error("Austra did not return a welcome message.");
    }
    tourState = result;
    tourStarting = false;
    connectionStatus(true);
    byId("messageInput").disabled = false;
    byId("sendButton").disabled = false;
    renderTour();
  } catch (error) {
    if (requestGeneration !== authGeneration) return;
    tourStarting = false;
    byId("messages").replaceChildren();
    const card = tourBubble("I couldn’t start our conversation yet. Please try again.");
    const retry = add(card, "button", "Try again", "inline-button");
    retry.type = "button";
    retry.onclick = () => { retry.disabled = true; startTour(); };
    status(error.message, true);
  }
}

async function finishTour() {
  byId("appShell").classList.remove("tour-mode");
  byId("messageInput").disabled = false;
  byId("sendButton").disabled = busy;
  byId("messageInput").placeholder = "Ask Austra a question…";
  try { await refreshConfig(); }
  catch (error) { status(error.message, true); return; }
  status("You can update these details in Settings → Org info.");
  renderOnboardingChat();
  await loadChats();
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

function closeChatMenus(restoreFocus = false) {
  const openMenus = byId("chatList").querySelectorAll(".chat-menu:not([hidden])");
  openMenus.forEach(menu => {
    menu.hidden = true;
    const trigger = menu.previousElementSibling;
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
  });
  return openMenus.length > 0;
}

function syncChatSelection() {
  byId("chatList").querySelectorAll(".chat-link").forEach(button => {
    const active = button.dataset.chatId
      ? button.dataset.chatId === currentChatId
      : button.dataset.chatMode === organizationChatMode && !currentChatId;
    button.classList.toggle("active", active);
    button.closest(".chat-list-item").classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

async function addPendingFiles(files) {
  if (busy || fileSelectionBusy) return;
  fileSelectionBusy = true;
  byId("sendButton").disabled = true;
  let duplicates = 0;
  try {
    for (const file of files) {
      if (!/\.(pdf|docx|csv|txt|md|xlsx|png|jpe?g|webp)$/i.test(file.name)) {
        status("Attach a PDF, DOCX, CSV, TXT, MD, XLSX, PNG, JPG, or WEBP file.", true);
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        status(`${file.name} exceeds 5 MB.`, true);
        continue;
      }
      if (/\.(png|jpe?g|webp)$/i.test(file.name)
        && file.size + pendingFiles.filter(item => /\.(png|jpe?g|webp)$/i.test(item.file.name))
          .reduce((total, item) => total + item.file.size, 0) > MAX_IMAGE_REQUEST_BYTES) {
        status("Images in one message cannot exceed 18 MB total.", true);
        continue;
      }
      const bytes = new Uint8Array(await file.arrayBuffer());
      const hash = crypto.subtle
        ? Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
            .map(value => value.toString(16).padStart(2, "0")).join("")
        : `${file.name}:${file.size}:${file.lastModified}`;
      if (pendingFiles.some(item => item.digest === hash
        || item.file.name.toLocaleLowerCase() === file.name.toLocaleLowerCase()
        || (!crypto.subtle && item.bytes?.length === bytes.length
          && item.bytes.every((value, index) => value === bytes[index])))) {
        duplicates += 1;
        continue;
      }
      if (pendingFiles.length >= MAX_ATTACHMENTS) {
        status(`Attach up to ${MAX_ATTACHMENTS} files at once.`, true);
        break;
      }
      pendingFiles.push({ file, digest: hash, bytes: crypto.subtle ? null : bytes });
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

async function uploadOrganizationFiles(files) {
  const requestGeneration = authGeneration;
  const sourceIds = [];
  for (const { file } of files) {
    status(`Uploading ${file.name}…`);
    const form = new FormData();
    form.append("upload", file, file.name);
    const response = await fetch("/api/org/onboarding-sources/files", {
      method: "POST", credentials: "same-origin",
      headers: { "X-Austra-Request": "1" }, body: form
    });
    const saved = await response.json();
    if (requestGeneration !== authGeneration) throw new Error("Your session ended. Sign in again.");
    if (response.status === 401) {
      showSignedOut();
      throw new Error("Your session ended. Sign in again.");
    }
    if (!response.ok) throw new Error(typeof saved.detail === "string" ? saved.detail : "File upload failed");
    status(`Reading ${file.name}…`);
    const processed = await api(`/api/org/onboarding-sources/revisions/${saved.revision_id}/process`, {
      method: "POST"
    });
    if (processed.state === "failed") {
      throw new Error(`${file.name}: ${processed.error || "Could not read this file"}`);
    }
    sourceIds.push(saved.source_id);
  }
  return sourceIds;
}

async function refreshConfig() {
  const wasAuthenticated = currentConfig?.authenticated;
  const config = await api("/api/config");
  if (wasAuthenticated && !config.authenticated) {
    showSignedOut();
    return;
  }
  currentConfig = config;
  if (config.authenticated && config.role === "org_admin" && config.legacy_workspace === false && !config.must_change_password) {
    const state = await api("/api/org/tour");
    tourState = typeof state.completed === "boolean" ? state : null;
  } else tourState = null;
  const touring = Boolean(tourState && !tourState.completed);
  byId("appShell").classList.toggle("tour-mode", touring);
  byId("organizationLabel").textContent = config.organization_name || "Business workspace";
  byId("sidebarOrganization").textContent = byId("organizationLabel").textContent;
  const canManageOrganization = config.saas_mode && config.role === "org_admin" && !touring;
  byId("organizationNavigation").hidden = !canManageOrganization;
  byId("departmentsButton").hidden = !canManageOrganization;
  byId("profileButton").hidden = !tourState;
  byId("orgInfoButton").hidden = !canManageOrganization;
  const legacyWorkspace = config.legacy_workspace !== false;
  const canManage = canManageConnections();
  byId("sidebarPages").hidden = !canManage;
  byId("workspaceButton").hidden = !legacyWorkspace || !canManage;
  byId("infrastructureButton").hidden = !legacyWorkspace || !canManage;
  byId("connectionsButton").hidden = !canManage;
  byId("topConnections").hidden = !canManage;
  const connectionsLabel = legacyWorkspace ? "Connections" : "Model connections";
  byId("connectionsButton").textContent = connectionsLabel;
  byId("connectionsTitle").textContent = connectionsLabel;
  byId("topConnections").textContent = connectionsLabel;
  byId("attachButton").hidden = !legacyWorkspace && config.role !== "org_admin";
  byId("chatList").hidden = !legacyWorkspace && config.role === "org_admin" && !tourState?.completed;
  byId("chatListLabel").hidden = byId("chatList").hidden;
  await loadAskDepartments();
  byId("appShell").classList.toggle(
    "signed-out", !config.authenticated || config.must_change_password || config.role === "super_admin"
  );
  clearTimeout(sessionExpiryTimer);
  if (config.authenticated) {
    sessionExpiryTimer = setTimeout(
      () => showSignedOut(), Math.ceil(config.session_seconds_remaining * 1000)
    );
  }
  if (!currentConfig.connectors.anthropic) modelConnectionVerified = false;
  byId("connection").textContent = !currentConfig.connectors.anthropic
    ? "Austra model setup needed"
    : modelConnectionVerified ? "Austra model connected"
    : config.role === "employee" && !legacyWorkspace ? "Austra model configured" : "Austra model saved · checking";
  byId("connection").classList.toggle("ready", modelConnectionVerified);
}

async function loadAskDepartments() {
  const selector = byId("askDepartment");
  if (!selector) return;
  selector.hidden = currentConfig?.legacy_workspace !== false || currentConfig?.role !== "employee"
    || !currentConfig.authenticated || currentConfig.must_change_password;
  if (selector.hidden) { askDepartmentId = ""; return; }
  const departments = await api("/api/org/ask/departments");
  selector.replaceChildren();
  if (departments.length !== 1) {
    const option = add(selector, "option", departments.length ? "Choose department" : "No department access");
    option.value = "";
  }
  departments.forEach(department => {
    const option = add(selector, "option", department.name);
    option.value = department.id;
  });
  askDepartmentId = departments.some(item => item.id === askDepartmentId)
    ? askDepartmentId : departments.length === 1 ? departments[0].id : "";
  selector.value = askDepartmentId;
  selector.onchange = () => { askDepartmentId = selector.value; };
}

function connectionStatus(connected) {
  modelConnectionVerified = connected;
  byId("connection").textContent = connected ? "Austra model connected" : "Austra model connection failed";
  byId("connection").classList.toggle("ready", connected);
}

function showError(message) {
  status(message, true);
  showModal({ title: "Something went wrong", message });
}

async function testSavedModel() {
  if (currentConfig?.role === "employee" && currentConfig.legacy_workspace === false) return;
  try {
    await api("/api/connections/anthropic/test", { method: "POST" });
    connectionStatus(true);
  } catch (error) {
    if (!currentConfig?.authenticated) return;
    connectionStatus(false);
    status(error.message, true);
    securePrompt("anthropic");
  }
}

function securePrompt(connector) {
  if (!currentConfig.guides[connector]) return;
  if (!canManageConnections()) {
    status("Ask your organization admin to set up the model connection.", true);
    return;
  }
  const card = bubble(`${currentConfig.guides[connector].title} needs a connection.`);
  const button = add(card, "button", "Open Connections", "inline-button");
  button.type = "button";
  button.onclick = () => showConnections(connector);
}

function renderAccessForm(title, description) {
  const messages = byId("messages");
  messages.replaceChildren();
  messages.removeAttribute("role");
  messages.removeAttribute("aria-live");
  const card = add(messages, "section", "", "sign-in-card");
  const brand = add(card, "div", "", "brand");
  add(brand, "span", "A", "brand-mark").setAttribute("aria-hidden", "true");
  add(brand, "span", "Austra");
  add(card, "h1", title).id = "accessTitle";
  document.title = `${title} · Austra`;
  byId("chatArea").setAttribute("aria-labelledby", "accessTitle");
  add(card, "p", description);
  return add(card, "form");
}

function accessField(form, labelText, autocomplete, type = "password") {
  const label = add(form, "label", labelText);
  const input = add(label, "input");
  input.type = type;
  input.autocomplete = autocomplete;
  input.required = true;
  return input;
}

function accessPrompt(setup) {
  byId("appShell").classList.add("signed-out");
  showChatView(false);
  const form = renderAccessForm(setup ? "Create your workspace" : "Sign in", currentConfig?.saas_mode
    ? `Use your account for ${currentConfig.organization_slug === "platform" ? "the Austra platform" : window.location.host}.`
    : setup
      ? "Choose an administrator passphrase to protect chats, approvals, and credentials."
      : "Enter your administrator passphrase to open your workspace.");
  let username = null;
  if (currentConfig?.saas_mode) {
    username = accessField(form, "Username", "username", "text");
    username.value = currentConfig.organization_slug === "platform" ? "superadmin" : "admin";
  }
  const input = accessField(form, currentConfig?.saas_mode ? "Password" : "Administrator passphrase",
    setup ? "new-password" : "current-password");
  if (setup) input.placeholder = "At least 12 characters";
  const button = add(form, "button", setup ? "Create workspace" : "Sign in", "primary");
  button.type = "submit";
  const feedback = add(form, "p", "", "auth-feedback");
  feedback.setAttribute("role", "alert");
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    feedback.textContent = "";
    try {
      await api(setup ? "/api/setup" : "/api/login", {
        method: "POST", body: JSON.stringify({
          password: input.value,
          ...(username ? { username: username.value } : {})
        })
      });
      input.value = "";
      feedback.textContent = "";
      status("");
      await refreshConfig();
      await enterAustra();
    } catch (error) {
      feedback.textContent = error.message;
    } finally { button.disabled = false; }
  };
  input.focus();
}

function employeeHasNoChatDepartment() {
  return currentConfig?.legacy_workspace === false && currentConfig.role === "employee"
    && !Array.from(byId("askDepartment").options).some(option => option.value);
}

async function loadChats() {
  const list = byId("chatList");
  if (employeeHasNoChatDepartment()) { list.replaceChildren(); return; }
  if (!list.childElementCount) showLoadingState(list, "Loading chats", "list");
  let chats;
  const chatsPath = currentConfig?.legacy_workspace === false ? "/api/org/chats" : "/api/chats";
  try { chats = await api(chatsPath); }
  catch (error) { list.replaceChildren(); throw error; }
  if (currentConfig?.saas_mode && currentConfig.legacy_workspace !== false) {
    const workflowChats = await api("/api/org/workflow-chats").catch(() => []);
    if (Array.isArray(workflowChats)) chats = [...chats, ...workflowChats]
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  }
  const scrollTop = list.scrollTop;
  list.replaceChildren();
  if (currentConfig?.legacy_workspace === false && tourState?.completed
    && !tourState.discovery_state?.chat_deleted) {
    const row = add(list, "div", "", "chat-list-item");
    const title = `${tourState.organization_name} onboarding`;
    const button = add(row, "button", title,
      `chat-link${organizationChatMode === "discovery" && !currentChatId ? " active" : ""}`);
    button.type = "button";
    button.title = title;
    button.setAttribute("aria-label", title);
    button.dataset.chatMode = "discovery";
    button.onclick = renderOnboardingChat;
    const more = add(row, "button", "⋯", "chat-menu-trigger");
    more.type = "button";
    more.title = "Chat options";
    more.setAttribute("aria-label", `Options for ${title}`);
    more.setAttribute("aria-expanded", "false");
    const menu = add(row, "div", "", "chat-menu");
    menu.hidden = true;
    more.onclick = event => {
      event.stopPropagation();
      const opening = menu.hidden;
      closeChatMenus();
      menu.hidden = !opening;
      more.setAttribute("aria-expanded", String(opening));
      if (opening) menu.classList.toggle("above",
        row.getBoundingClientRect().bottom + menu.offsetHeight > list.getBoundingClientRect().bottom);
    };
    const remove = add(menu, "button", "Delete chat", "chat-menu-delete");
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete chat ${title}`);
    remove.onclick = async () => {
      closeChatMenus();
      more.focus();
      if (!await showModal({
        title: "Delete chat?",
        message: "Remove this onboarding conversation and its saved memory? Your confirmed company details and workflow drafts will remain.",
        confirmLabel: "Delete chat",
        cancelLabel: "Cancel",
        destructive: true
      })) return;
      try {
        tourState = await api("/api/org/discovery/chat", { method: "DELETE" });
        if (organizationChatMode === "discovery" && !currentChatId) renderWelcome();
        await loadChats();
        status("Onboarding chat deleted.");
      } catch (error) { status(error.message, true); }
    };
  }
  if (!chats.length && !list.childElementCount) add(list, "p", "No chats yet.", "chat-list-empty");
  chats.forEach(chat => {
    const row = add(list, "div", "", "chat-list-item");
    const button = add(row, "button", chat.title, `chat-link${chat.id === currentChatId ? " active" : ""}`);
    button.type = "button";
    button.title = chat.title;
    button.setAttribute("aria-label", chat.title);
    button.dataset.chatId = chat.id;
    button.onclick = () => showChat(chat.id, true, Boolean(chat.workflow));
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
        await api(`${chat.workflow ? "/api/org/chats" : chatsPath}/${chat.id}`, { method: "DELETE" });
        if (currentChatId === chat.id) renderWelcome();
        await loadChats();
        status("Chat removed from the list. Its audit copy remains.");
      } catch (error) { status(error.message, true); }
    };
  });
  syncChatSelection();
  list.scrollTop = scrollTop;
}

document.addEventListener("click", event => {
  if (!event.target.closest(".chat-list-item")) closeChatMenus();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !event.defaultPrevented) closeChatMenus(true);
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
  workflowConversation = null;
  const previousThread = byId("thread");
  const existing = previousThread
    ? [...previousThread.querySelectorAll(".message-row > .bubble[data-message-id]")]
    : [];
  const canAppend = currentChatId === chat.id && Boolean(previousThread)
    && existing.length <= chat.messages.length
    && existing.every((item, index) => item.dataset.messageId === chat.messages[index].id);
  currentChatId = chat.id;
  syncChatSelection();
  byId("chatTitle").textContent = chat.title;
  if (canAppend) {
    previousThread.querySelectorAll(".message-row").forEach(row => {
      if (!row.querySelector(".bubble[data-message-id]")) row.remove();
    });
    previousThread.querySelectorAll(".a2ui-item, .a2ui-panel").forEach(item => item.remove());
  } else byId("messages").replaceChildren();
  chat.messages.slice(canAppend ? existing.length : 0).forEach(message => {
    const visual = message.role === "assistant" && (message.report || message.visualize);
    const showVisual = visual && Boolean(window.AustraA2UI);
    const item = bubble(
      visual && !showVisual ? message.body : message.display_body ?? message.body,
      message.role
    );
    item.dataset.messageId = message.id;
    if (canAppend && message.role === "assistant") item.parentElement.classList.add("arriving");
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
  if (!chat.workflow && currentConfig?.legacy_workspace !== false) {
    const reviewPanel = add(thread(), "div", "", "a2ui-panel");
    loadChatSurfaces(chat.id, reviewPanel, surfaceRequest, result.drafts || [])
      .catch(error => status(error.message, true));
  }
  if (chat.workflow) workflowConversation = mountWorkflowConversation(chat, result.workflowPrompt, {
    isCurrent: () => currentChatId === chat.id && currentConfig?.authenticated,
    legacy: currentConfig?.legacy_workspace === true,
  });
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
  if (!byId("workspacePage").hidden) {
    workspaceFeedback(`Draft ${decision}.`);
    await loadWorkspace();
  } else if (!byId("chatArea").hidden) {
    status(`Draft ${decision}.`);
    if (currentChatId) await showChat(currentChatId);
  }
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
      if (description.kind === "workspace_consent") {
        if (messageBubble?.parentElement.classList.contains("assistant")) {
          mountWorkspaceConsent(messageBubble, description);
        }
        return;
      }
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
        if (description.can_prepare_workflow && currentConfig?.saas_mode) {
          const prepare = add(holder, "a", "Prepare purchase workflow", "top-action");
          prepare.href = "/organization/workflow-reviews?purchase_draft=" + description.id.slice("draft_".length);
        }
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

async function showChat(chatId, activate = true, workflow = false) {
  const requestId = ++chatOpenRequest;
  try {
    if (chatId !== currentChatId) clearPendingFiles();
    if (currentConfig?.legacy_workspace === false) organizationChatMode = "knowledge";
    if (activate) showChatView();
    const chatsPath = workflow || currentConfig?.legacy_workspace === false ? "/api/org/chats" : "/api/chats";
    const result = await api(`${chatsPath}/${chatId}`);
    if (requestId !== chatOpenRequest) return;
    renderChat(result);
  } catch (error) { status(error.message, true); }
}

async function sendMessage() {
  if (busy || fileSelectionBusy || !currentConfig?.authenticated) return;
  const input = byId("messageInput");
  const text = input.value.trim();
  const selectedFiles = [...pendingFiles];
  const workflowSelection = byId("discoveryWorkflow")?.value;
  if (tourState && !tourState.completed) {
    if (!currentConfig.connectors.anthropic || tourStarting || (!text && !selectedFiles.length)) return;
    const messageBody = text || "Please read the attached files.";
    busy = true;
    byId("sendButton").disabled = true;
    input.disabled = true;
    byId("attachButton").disabled = true;
    byId("messages").setAttribute("aria-busy", "true");
    input.value = "";
    clearPendingFiles();
    const optimistic = bubble(messageBody, "user");
    selectedFiles.forEach(({ file }) => add(optimistic, "div", `📎 ${file.name}`, "sent-attachment"));
    const thinking = thinkingBubble();
    setThinkingStage(thinking, selectedFiles.length ? "Reading attached files" : "Checking company details");
    try {
      const attachments = await uploadOrganizationFiles(selectedFiles);
      tourState = await postStream("/api/org/tour/message/stream", {
        body: messageBody, revision: tourState.revision, attachments
      }, (event, data) => {
        if (event === "stage") setThinkingStage(thinking, data.stage);
      });
      connectionStatus(true);
      if (tourState.completed) {
        thinking.remove();
        tourBubble(tourState.messages.at(-1).body);
        await finishTour();
      } else renderTour();
    } catch (error) {
      thinking.remove();
      optimistic.parentElement.remove();
      input.value = text;
      pendingFiles = selectedFiles;
      renderPendingFiles();
      status(error.message, true);
    } finally {
      busy = false;
      input.disabled = false;
      byId("attachButton").disabled = false;
      byId("messages").removeAttribute("aria-busy");
      byId("sendButton").disabled = !currentConfig?.authenticated
        || !currentConfig.connectors.anthropic || tourStarting;
    }
    return;
  }
  if (!text && !selectedFiles.length) return;
  if (/sk-ant-|xox[baprs]-|-----BEGIN .*PRIVATE KEY-----/i.test(text)) {
    status("Use Connections to enter credentials privately.", true);
    return;
  }
  busy = true;
  const requestGeneration = authGeneration;
  byId("sendButton").disabled = true;
  byId("messages").setAttribute("aria-busy", "true");
  status("");
  input.value = "";
  resizeMessageInput();
  clearPendingFiles();
  const optimistic = bubble(text || "Please read the attached files.", "user");
  selectedFiles.forEach(({ file }) => add(optimistic, "div", `📎 ${file.name}`, "sent-attachment"));
  const thinking = thinkingBubble();
  let streamedAnswer = null;
  let acceptedChatId = null;
  scrollBottom();
  try {
    if (currentConfig?.saas_mode && !selectedFiles.length && !text.startsWith("/capability ")) {
      if (workflowConversation?.active()) {
        thinking.remove();
        await workflowConversation.respond(text);
        return;
      }
      if (typeof wantsWorkflowChat === "function" && wantsWorkflowChat(text)) {
        const result = await api("/api/org/workflow-chats", {
          method: "POST", body: JSON.stringify({message: text})
        });
        if (requestGeneration === authGeneration) {
          thinking.remove();
          renderChat({...result, workflowPrompt: text});
        }
        return;
      }
    }
    if (currentConfig.legacy_workspace === false && tourState?.completed
      && currentConfig.role === "org_admin"
      && (organizationChatMode === "discovery" || selectedFiles.length > 0)
      && !text.startsWith("/capability ")) {
      const attachments = await uploadOrganizationFiles(selectedFiles);
      tourState = await postStream("/api/org/discovery/message/stream", {
        ...(workflowSelection === "new" ? { new_workflow: true }
          : workflowSelection ? { workflow_case_id: workflowSelection } : {}),
        body: text || "Please read the attached files.", revision: tourState.revision, attachments
      }, (event, data) => {
        if (event === "stage") setThinkingStage(thinking, data.stage);
      });
      connectionStatus(true);
      if (requestGeneration !== authGeneration) return;
      thinking.remove();
      renderOnboardingChat(workflowSelection);
      await loadChats();
      status("");
      return;
    }
    if (currentConfig.legacy_workspace === false && !text.startsWith("/capability ")) {
      if (currentConfig.role === "employee" && !askDepartmentId) {
        if (employeeHasNoChatDepartment()) throw new Error("Ask your organization admin for departmental chat access.");
        throw new Error("Choose a department before asking a question.");
      }
      const result = await postStream("/api/org/ask/stream", {
        question: text, chat_id: currentChatId, department_id: askDepartmentId || null
      }, (event, data) => {
        if (event === "accepted") acceptedChatId = data.chat_id;
        if (event === "stage") status(data.stage);
        if (event !== "delta") return;
        thinking.remove();
        if (!streamedAnswer) {
          streamedAnswer = bubble("", "assistant");
          streamedAnswer.parentElement.classList.add("streaming-row");
          add(streamedAnswer, "p", "", "streaming-text");
        }
        streamedAnswer.querySelector(".streaming-text").textContent += data.text;
        scrollBottom();
      });
      if (requestGeneration !== authGeneration) return;
      thinking.remove();
      renderChat({ chat: result.chat, drafts: [] });
      await loadChats();
      status("");
      return;
    }
    if (currentConfig?.saas_mode && text.startsWith("/capability ")) {
      if (selectedFiles.length) throw new Error("Capability commands cannot include attachments.");
      const command = JSON.parse(text.slice("/capability ".length));
      const decision = await api("/api/org/capability-chat/commands", {
        method: "POST", body: JSON.stringify(command)
      });
      if (requestGeneration !== authGeneration) return;
      thinking.remove();
      const records = [decision.record, decision.order, decision.lot].filter(Boolean);
      const outcome = records.map(record => `${record.external_id}: ${record.state}`).join("; ");
      bubble(`${outcome || decision.effect || "Decision recorded"}. Event ${decision.event_id}.`, "assistant");
      status("");
      return;
    }
    const form = new FormData();
    form.append("body", text);
    if (currentChatId) form.append("chat_id", currentChatId);
    selectedFiles.forEach(({ file }) => form.append("uploads", file, file.name));
    const response = await fetch("/api/messages/background", {
      method: "POST", credentials: "same-origin", headers: { "X-Austra-Request": "1" }, body: form
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
      streamJob(result.job);
    }
    if (result.duplicate_files?.length) {
      status(`${result.duplicate_files.length} duplicate file${result.duplicate_files.length === 1 ? "" : "s"} skipped.`);
    } else if (!result.analysis_error) status("");
  } catch (error) {
    if (requestGeneration !== authGeneration) return;
    thinking.remove();
    streamedAnswer?.parentElement.remove();
    if (acceptedChatId) {
      await showChat(acceptedChatId, false);
      showError(error.message);
      return;
    }
    optimistic.parentElement.remove();
    input.value = text;
    resizeMessageInput();
    pendingFiles = selectedFiles;
    renderPendingFiles();
    if (/conversation changed|onboarding changed/i.test(error.message)) {
      try {
        tourState = await api("/api/org/tour");
        if (requestGeneration === authGeneration) {
          renderOnboardingChat();
          const selector = byId("discoveryWorkflow");
          if (workflowSelection && selector && [...selector.options].some(option => option.value === workflowSelection)) {
            selector.value = workflowSelection;
          }
          input.value = text;
          pendingFiles = selectedFiles;
          renderPendingFiles();
          status("The conversation was refreshed. Review your message and send it again.");
        }
      } catch (refreshError) { showError(refreshError.message); }
    } else showError(error.message);
  } finally {
    busy = false;
    byId("sendButton").disabled = !currentConfig?.authenticated;
    byId("messages").removeAttribute("aria-busy");
    if (currentConfig?.authenticated) input.focus();
  }
}

function organizationRoute() {
  const path = window.location.pathname;
  return ORGANIZATION_PAGES.has(path) || path.startsWith("/organization/departments/");
}

function canManageConnections() {
  return !currentConfig?.saas_mode || currentConfig.role === "org_admin";
}

function setAustraLocation(path) {
  const current = window.location.pathname + window.location.search;
  if (path !== current) window.history.pushState({}, "", path);
}

function syncAustraNavigation() {
  let path = window.location.pathname;
  if (path.startsWith("/organization/departments/")) path = "/organization/departments";
  if (path === "/organization/capability") path = "/organization/workflows";
  if (path === "/organization/onboarding") {
    const query = new URLSearchParams(window.location.search);
    if (query.has("workflow") || query.has("purchase_draft")) path = "/organization/workflow-reviews";
  }
  let label = "Chat";
  byId("sidebar").querySelectorAll("[data-austra-route]").forEach(link => {
    if (link.getAttribute("href") === path) {
      link.setAttribute("aria-current", "page");
      label = link.textContent;
    } else link.removeAttribute("aria-current");
  });
  byId("mobilePageLabel").textContent = label;
  document.title = `${label} · Austra`;
}

function setMobileNavigation(open, restoreFocus = false) {
  const sidebar = byId("sidebar");
  if (sidebar.classList.contains("open") === open) return;
  sidebar.classList.toggle("open", open);
  byId("menuButton").setAttribute("aria-expanded", String(open));
  byId("navigationBackdrop").hidden = !open;
  byId("mobileBar").toggleAttribute("inert", open);
  document.querySelectorAll("#appShell > main").forEach(page => page.toggleAttribute("inert", open));
  if (open) {
    sidebar.setAttribute("role", "dialog");
    sidebar.setAttribute("aria-modal", "true");
    byId("closeNavigation").focus();
  } else {
    sidebar.removeAttribute("role");
    sidebar.removeAttribute("aria-modal");
    closeChatMenus();
    if (restoreFocus) byId("menuButton").focus();
  }
}

function showMainPage(pageId) {
  const leavingDrawer = byId("sidebar").classList.contains("open");
  document.querySelectorAll("#appShell > main").forEach(page => {
    page.hidden = page.id !== pageId;
  });
  setMobileNavigation(false);
  syncAustraNavigation();
  if (leavingDrawer && !byId("appShell").classList.contains("signed-out")) {
    byId(pageId).focus({ preventScroll: true });
  }
}

function showChatView(syncRoute = true) {
  if (syncRoute && window.location.pathname !== "/") setAustraLocation("/");
  showMainPage("chatArea");
  scrollBottom();
}

async function renderAustraRoute() {
  if (!currentConfig?.authenticated || currentConfig.must_change_password || currentConfig.role === "super_admin") return;
  if (tourState && !tourState.completed && window.location.pathname !== "/profile") {
    renderTour();
    return;
  }
  const path = window.location.pathname;
  if (path === "/") { showChatView(false); return; }
  if (path === "/connections" && canManageConnections()) {
    showConnections(new URLSearchParams(window.location.search).get("connector") || "anthropic", false);
    return;
  }
  if (path === "/profile" && tourState) { showAccount("profile"); return; }
  if (path === "/organization/workflow-planning" && currentConfig.saas_mode) {
    await showOrganizationManagement();
    return;
  }
  if (currentConfig.legacy_workspace !== false && canManageConnections()) {
    if (path === "/workspace") { showWorkspace(); return; }
    if (path === "/infrastructure") { await showInfrastructure(); return; }
  }
  if (organizationRoute() && currentConfig.saas_mode && currentConfig.role === "org_admin") {
    if (path !== "/organization/info") { await showOrganizationManagement(); return; }
    showAccount("organization");
    return;
  }
  window.history.replaceState({}, "", "/");
  showChatView(false);
  status("This page is unavailable for your account.", true);
}

async function navigateAustra(path) {
  if (!currentConfig?.authenticated) return;
  setAustraLocation(path);
  await renderAustraRoute();
}

window.navigateAustra = navigateAustra;

function connectionFeedback(text, error = false, loading = false) {
  const feedback = byId("connectionFeedback");
  if (!feedback) return;
  feedback.textContent = text;
  feedback.className = `connection-feedback${error ? " error" : ""}${loading ? " loading" : ""}`;
}

function wireConnectionOption(button, connector) {
  button.type = "button";
  button.dataset.connector = connector;
  button.setAttribute("aria-pressed", String(connector === selectedConnector));
  button.onclick = () => {
    if (connector === selectedConnector) return;
    showConnections(connector);
    [...byId("connectionList").children].find(option => option.dataset.connector === connector)?.focus();
  };
}

function renderConnectionList() {
  const list = byId("connectionList");
  list.replaceChildren();
  Object.entries(currentConfig.guides).forEach(([name, guide]) => {
    if (currentConfig.legacy_workspace === false && name !== "anthropic") return;
    const button = add(list, "button", "", `connection-option${name === selectedConnector ? " active" : ""}`);
    add(button, "strong", guide.title);
    add(button, "small", currentConfig.connectors[name] ? "Saved" : "Setup needed");
    wireConnectionOption(button, name);
  });
  if (currentConfig.legacy_workspace === false) return;
  const mcpButton = add(list, "button", "", `connection-option${selectedConnector === "mcp" ? " active" : ""}`);
  add(mcpButton, "strong", "Other apps via MCP");
  add(mcpButton, "small", "Add a reusable server");
  wireConnectionOption(mcpButton, "mcp");
}

async function testConnection(connector) {
  const button = byId("testConnection");
  const isCurrent = () => button.isConnected && !byId("connectionsPage").hidden;
  button.disabled = true;
  button.textContent = "Testing…";
  connectionFeedback("Testing connection…", false, true);
  try {
    await api(`/api/connections/${connector}/test`, { method: "POST" });
    if (connector === "anthropic") connectionStatus(true);
    if (!isCurrent()) return;
    if (connector === "slack") {
      await refreshConfig();
      if (!isCurrent()) return;
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
    if (isCurrent()) connectionFeedback(error.message, true);
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
  const isCurrent = () => form.isConnected && !byId("connectionsPage").hidden;
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
      if (!isCurrent()) return;
      await renderMcpDetail();
      if (selectedConnector === "mcp") connectionFeedback(`${payload.name} connected. Its tools are ready.`);
    } catch (error) {
      if (isCurrent()) connectionFeedback(error.message, true);
      save.disabled = false;
    }
  };
  try {
    const connections = await api("/api/mcp/connections");
    if (!isCurrent()) return;
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
          if (!isCurrent()) return;
          connectionFeedback(`${name}: ${result.tools.length} tools discovered.`);
          if (!connection.built_in) await renderMcpDetail();
        } catch (error) { if (isCurrent()) connectionFeedback(error.message, true); }
      };
      if (!connection.built_in) {
        const remove = add(row, "button", "Remove", "top-action");
        remove.type = "button";
        remove.onclick = async () => {
          try {
            await api(`/api/mcp/connections/${name}`, { method: "DELETE" });
            if (!isCurrent()) return;
            await renderMcpDetail();
            if (selectedConnector === "mcp") connectionFeedback(`${name} removed.`);
          } catch (error) { if (isCurrent()) connectionFeedback(error.message, true); }
        };
      }
    });
  } catch (error) { if (isCurrent()) connectionFeedback(error.message, true); }
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
    if (currentConfig.saas_mode && currentConfig.connectors.gmail) {
      const linkWorkflow = add(actions, "button", "Use Gmail in released workflows", "top-action");
      linkWorkflow.type = "button";
      linkWorkflow.onclick = async () => {
        linkWorkflow.disabled = true;
        try {
          await api("/api/connections/gmail/link-workflow", {method: "POST"});
          connectionFeedback("Gmail verified for workflow setup. Email actions still require approval.");
        } catch (error) { connectionFeedback(error.message, true); }
        finally { linkWorkflow.disabled = false; }
      };
    }
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
      if (connector === "gmail") {
        connectionFeedback("OAuth client saved. Choose Connect with Google.");
        return;
      }
      connectionFeedback(currentConfig.connectors[connector]
        ? "Credentials saved. Testing connection…"
        : "Saved. Complete the remaining fields to test.");
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
  window.history.replaceState({}, "", "/connections?connector=gmail");
  showConnections("gmail", false);
  connectionFeedback(query.get("oauth") === "connected"
    ? "Gmail connected and mailbox verified."
    : "Gmail authorization was not completed. Choose Connect with Google to try again.",
  query.get("oauth") !== "connected");
}

function showConnections(connector = "anthropic", syncRoute = true) {
  if (!currentConfig?.authenticated || !canManageConnections()) return;
  if (tourState && !tourState.completed) { renderTour(); return; }
  selectedConnector = currentConfig.legacy_workspace === false ? "anthropic"
    : connector === "mcp" || currentConfig.guides[connector] ? connector : "anthropic";
  if (syncRoute) setAustraLocation(selectedConnector === "anthropic"
    ? "/connections" : `/connections?connector=${encodeURIComponent(selectedConnector)}`);
  showMainPage("connectionsPage");
  renderConnectionList();
  renderConnectionDetail();
}

function renderWorkspaceTabs() {
  const tabs = byId("workspaceTabs");
  if (!tabs.childElementCount) {
    Object.entries(WORKSPACE_SECTIONS).forEach(([section, details]) => {
      const button = add(tabs, "button", details.label);
      button.type = "button";
      button.dataset.section = section;
      button.onclick = () => {
        if (workspaceSection === section) return;
        workspaceSection = section;
        workspaceOffset = 0;
        renderWorkspaceTabs();
        loadWorkspace();
      };
    });
  }
  [...tabs.children].forEach(button => {
    const active = button.dataset.section === workspaceSection;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function renderPageState(content, title, description, retry) {
  content.replaceChildren();
  const panel = add(content, "section", "", `page-state${retry ? " page-state-error" : ""}`);
  const text = add(panel, "div");
  text.setAttribute("role", retry ? "alert" : "status");
  add(text, "h2", title);
  add(text, "p", description);
  if (retry) {
    const button = add(panel, "button", "Retry", "top-action");
    button.type = "button";
    button.onclick = retry;
  }
}

async function loadWorkspace(focusResults = false) {
  const request = ++workspaceRequest;
  const section = workspaceSection;
  const offset = workspaceOffset;
  const departmentId = workspaceDepartmentId;
  const content = byId("workspaceContent");
  const pagination = byId("workspacePagination");
  const isCurrent = () => request === workspaceRequest && !byId("workspacePage").hidden;
  pagination.hidden = true;
  content.setAttribute("aria-busy", "true");
  showLoadingState(content, "Loading workspace", "workspace");
  try {
    const response = await api(`/api/ui/workspace?section=${section}&offset=${offset}&department_id=${encodeURIComponent(departmentId)}`);
    if (!isCurrent()) return;
    if (offset && offset >= response.total) {
      workspaceOffset = Math.max(0, Math.floor((response.total - 1) / response.page_size) * response.page_size);
      return loadWorkspace(focusResults);
    }
    if (!response.total) {
      const empty = WORKSPACE_SECTIONS[section];
      renderPageState(content, empty.empty, empty.description);
      return;
    }
    if (!window.AustraA2UI) throw new Error("The workspace renderer could not load.");
    content.replaceChildren();
    const pageItemIds = new Set(response.items.map(item => item.id).filter(Boolean));
    window.AustraA2UI.mount(content, response.surface, async action => {
      const decisions = { approve_draft: "approved", reject_draft: "rejected" };
      const draftId = action.context?.draft_id;
      if (!isCurrent() || !decisions[action.name] || !pageItemIds.has(draftId)) return;
      try { await reviewDraft(draftId, decisions[action.name]); }
      catch (error) { if (isCurrent()) workspaceFeedback(error.message, true); }
    });
    const sourceItems = response.items.filter(item => typeof item.source === "string");
    if (sourceItems.length) {
      const sources = add(content, "details", "", "workspace-sources");
      add(sources, "summary", `Source material (${sourceItems.length})`);
      sourceItems.forEach(item => {
        const details = add(sources, "details", "", "a2ui-source");
        add(details, "summary", item.title);
        add(details, "pre", item.source, "skill-source");
      });
    }
    renderWorkspacePagination(response, offset);
  } catch (error) {
    if (isCurrent()) {
      renderPageState(content, "Workspace unavailable", error.message, () => loadWorkspace(true));
    }
  } finally {
    if (isCurrent()) {
      content.removeAttribute("aria-busy");
      content.scrollTop = 0;
      if (focusResults && document.activeElement === document.body) content.focus({ preventScroll: true });
    }
  }
}

function renderWorkspacePagination(response, offset) {
  const pagination = byId("workspacePagination");
  pagination.replaceChildren();
  const shown = Math.min(response.total, offset + response.page_size);
  const count = add(pagination, "span", `${offset + 1}–${shown} of ${response.total}`);
  count.setAttribute("role", "status");
  const previous = add(pagination, "button", "Previous");
  previous.type = "button";
  previous.disabled = offset === 0;
  previous.onclick = () => {
    workspaceOffset = Math.max(0, offset - response.page_size);
    loadWorkspace(true);
  };
  const next = add(pagination, "button", "Next");
  next.type = "button";
  next.disabled = shown >= response.total;
  next.onclick = () => {
    workspaceOffset = offset + response.page_size;
    loadWorkspace(true);
  };
  pagination.hidden = false;
}

function showWorkspace() {
  showMainPage("workspacePage");
  workspaceFeedback("");
  renderWorkspaceTabs();
  loadWorkspaceDepartments().catch(error => {
    if (!byId("workspacePage").hidden) workspaceFeedback(error.message, true);
  });
  loadWorkspace();
}

function workspaceFeedback(message, error = false) {
  const feedback = byId("workspaceFeedback");
  feedback.textContent = message;
  feedback.classList.toggle("error", error);
}

async function loadWorkspaceDepartments() {
  const selector = byId("workspaceDepartment");
  selector.hidden = !currentConfig?.saas_mode;
  if (!currentConfig?.saas_mode) return;
  const departments = await api("/api/org/departments");
  selector.replaceChildren();
  const all = add(selector, "option", "All departments");
  all.value = "";
  departments.filter(item => !item.archived).forEach(item => {
    const option = add(selector, "option", item.name);
    option.value = item.id;
  });
  selector.value = workspaceDepartmentId;
}

async function showOrganizationManagement() {
  showMainPage("departmentsPage");
  try { await window.renderOrganizationManagement(); }
  catch (error) { if (!byId("departmentsPage").hidden) orgNotice(error.message, true); }
}

function showAccount(view) {
  if (!currentConfig?.authenticated || (view === "profile" && !tourState)) return;
  showMainPage("accountPage");
  const root = byId("accountContent");
  root.replaceChildren();
  const heading = add(root, "header", "", "account-heading");
  add(heading, "h1", view === "profile" ? "My profile" : "Organization info").id = "accountTitle";
  const back = add(heading, "button", "← Back to chat", "top-action");
  back.type = "button";
  back.onclick = () => { if (tourState && !tourState.completed) renderTour(); else showChatView(); };
  if (view === "profile") {
    [
      ["Name", tourState.full_name || "Not added yet"],
      ["Username", tourState.username],
      ["Email", tourState.email || "Not added yet"],
      ["Phone", tourState.contact_number || "Not added yet"],
      ["About me", tourState.about_admin || "Not added yet"],
      ["Organization", tourState.organization_name]
    ].forEach(([label, value]) => {
      const row = add(root, "div", "", "account-detail");
      add(row, "span", label);
      add(row, "strong", value);
    });
    return;
  }
  if (!tourState?.completed) {
    const details = add(root, "dl", "", "account-summary");
    add(details, "dt", "Organization name");
    add(details, "dd", currentConfig.organization_name || "Business workspace");
    add(details, "dt", "Workspace address");
    add(details, "dd", window.location.host);
    return;
  }
  add(root, "p", "Update your organization’s name, description, and public links.", "account-description");
  const form = add(root, "form", "", "account-form");
  const inputs = {};
  [
    ["name", "Organization name", tourState.organization_name, "text"],
    ["about_organization", "About your organization", tourState.about_organization, "textarea"],
    ["desired_outcome", "What Austra should help with", tourState.desired_outcome, "textarea"],
    ["website_url", "Website URL", tourState.website_url, "url"],
    ["social_url", "Social media page URL", tourState.social_url, "url"]
  ].forEach(([key, labelText, value, type]) => {
    const label = add(form, "label", labelText);
    const input = add(label, type === "textarea" ? "textarea" : "input", "", "secure-input");
    if (type !== "textarea") input.type = type;
    input.required = key === "name" || key === "about_organization";
    input.value = value || "";
    inputs[key] = input;
  });
  const save = add(form, "button", "Save changes", "primary account-save");
  save.type = "submit";
  const feedback = add(form, "p", "", "account-feedback");
  feedback.setAttribute("role", "status");
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true;
    try {
      tourState = await api("/api/org/info", { method: "PUT", body: JSON.stringify(
        Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]))
      ) });
      currentConfig.organization_name = tourState.organization_name;
      byId("organizationLabel").textContent = currentConfig.organization_name;
      byId("sidebarOrganization").textContent = currentConfig.organization_name;
      feedback.textContent = "Saved. Your organization info is up to date.";
    } catch (error) {
      feedback.textContent = error.message;
    } finally { save.disabled = false; }
  };
}

async function showInfrastructure() {
  showMainPage("infrastructurePage");
  const request = ++infrastructureRequest;
  const content = byId("infrastructureContent");
  const refresh = byId("refreshInfrastructure");
  const isCurrent = () => request === infrastructureRequest && !byId("infrastructurePage").hidden;
  refresh.disabled = true;
  showLoadingState(content, "Loading services", "workspace");
  try {
    const result = await api("/api/infrastructure");
    if (!isCurrent()) return;
    const services = Object.entries(result.services);
    if (!services.length) {
      renderPageState(content, "No optional services", "Services appear here when configured.");
      return;
    }
    content.replaceChildren();
    const panel = add(content, "section", "", "infrastructure-panel");
    add(panel, "h2", "Optional services");
    const list = add(panel, "dl", "", "service-list");
    services.forEach(([name, state]) => {
      const row = add(list, "div");
      add(row, "dt", name);
      add(row, "dd", state);
    });
    add(panel, "p", "Ask Austra in chat to draft a service change for review.");
  } catch (error) {
    if (isCurrent()) renderPageState(content, "Infrastructure unavailable", error.message, showInfrastructure);
  } finally {
    if (request === infrastructureRequest) refresh.disabled = false;
  }
}

byId("newChat").onclick = () => { if (currentConfig?.authenticated && (!tourState || tourState.completed)) { showChatView(); renderWelcome(); } };
byId("sendButton").onclick = sendMessage;
byId("messageInput").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
};
byId("messageInput").oninput = resizeMessageInput;
byId("attachButton").onclick = () => {
  if (currentConfig?.legacy_workspace === false && tourState?.completed
    && organizationChatMode !== "discovery") {
    renderOnboardingChat();
    status("Files are added to Plan a process as supporting evidence.");
  }
  byId("fileInput").click();
};
byId("fileInput").onchange = event => addPendingFiles([...event.target.files]);
byId("sidebar").addEventListener("click", event => {
  const link = event.target.closest("[data-austra-route]");
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  navigateAustra(link.getAttribute("href")).catch(error => status(error.message, true));
});
byId("workspaceDepartment").onchange = event => {
  workspaceDepartmentId = event.target.value;
  workspaceOffset = 0;
  workspaceFeedback("");
  loadWorkspace();
};
byId("workspaceBack").onclick = showChatView;
byId("topConnections").onclick = () => showConnections();
byId("backToChat").onclick = showChatView;
byId("refreshInfrastructure").onclick = showInfrastructure;
byId("menuButton").onclick = () => setMobileNavigation(true);
byId("closeNavigation").onclick = () => setMobileNavigation(false, true);
byId("navigationBackdrop").onclick = () => setMobileNavigation(false, true);
window.matchMedia?.("(max-width: 700px)").addEventListener?.("change", event => {
  if (!event.matches) setMobileNavigation(false);
});
byId("sidebar").addEventListener("keydown", event => {
  const drawerOpen = byId("sidebar").classList.contains("open");
  if (event.key === "Escape") {
    if (closeChatMenus(true)) {
      event.preventDefault();
      return;
    }
    const pages = byId("sidebarPages");
    if (pages.open && (drawerOpen || pages.contains(event.target))) {
      event.preventDefault();
      pages.open = false;
      byId("sidebarPagesToggle").focus();
    } else if (drawerOpen) {
      event.preventDefault();
      setMobileNavigation(false, true);
    }
  } else if (event.key === "Tab" && drawerOpen) {
    const visible = [...byId("sidebar").querySelectorAll("a[href], summary, button:not(:disabled)")]
      .filter(control => control.getClientRects().length);
    const end = event.shiftKey ? visible[0] : visible.at(-1);
    if (document.activeElement === end) {
      event.preventDefault();
      (event.shiftKey ? visible.at(-1) : visible[0]).focus();
    }
  }
});
byId("logoutButton").onclick = async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showSignedOut();
  } catch (error) { status(error.message, true); }
};

window.addEventListener("popstate", () => {
  renderAustraRoute().catch(error => status(error.message, true));
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && currentConfig?.authenticated) {
    refreshConfig().catch(error => status(error.message, true));
  }
});

function renderPasswordChange() {
  byId("appShell").classList.add("signed-out");
  showChatView(false);
  const form = renderAccessForm("Change your password", "Choose a new password before opening your workspace.");
  const current = accessField(form, "Temporary password", "current-password");
  const replacement = accessField(form, "New password", "new-password");
  replacement.placeholder = "At least 12 characters";
  const button = add(form, "button", "Change password", "primary");
  button.type = "submit";
  const feedback = add(form, "p", "", "auth-feedback");
  feedback.setAttribute("role", "alert");
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    feedback.textContent = "";
    try {
      await api("/api/auth/change-password", {
        method: "POST", body: JSON.stringify({
          current_password: current.value, new_password: replacement.value
        })
      });
      current.value = "";
      replacement.value = "";
      showSignedOut();
    } catch (error) { feedback.textContent = error.message; }
    finally { button.disabled = false; }
  };
  current.focus();
}

async function renderPlatform() {
  byId("appShell").hidden = true;
  byId("platformRoot").hidden = false;
  await window.renderPlatformDashboard();
}

function organizationUrl(slug) {
  const url = new URL("/", window.location.origin);
  url.hostname = `${slug}.localhost`;
  return url.href;
}

async function start() {
  try {
    await refreshConfig();
    if (!currentConfig.admin_ready) accessPrompt(true);
    else if (!currentConfig.authenticated) accessPrompt(false);
    else await enterAustra();
  } catch (error) { status(error.message, true); }
}

async function enterAustra() {
  if (!currentConfig.authenticated) return;
  if (currentConfig.must_change_password) { renderPasswordChange(); return; }
  if (currentConfig.role === "super_admin") { await renderPlatform(); return; }
  const legacy = currentConfig.legacy_workspace !== false;
  byId("chatArea").setAttribute("aria-labelledby", "chatTitle");
  byId("messages").setAttribute("role", "log");
  byId("messages").setAttribute("aria-live", "polite");
  renderWelcome();
  if (!tourState || tourState.completed) await loadChats().catch(error => status(error.message, true));
  if (!currentConfig.authenticated) return;
  if (legacy) startProcessPolling();
  await renderAustraRoute();
  if (tourState && !tourState.completed) return;
  if (currentConfig.saas_mode && typeof startWorkflowAlerts === "function") {
    stopWorkflowAlerts?.();
    const session = authGeneration;
    stopWorkflowAlerts = startWorkflowAlerts(() => session === authGeneration && currentConfig?.authenticated);
  }
  if (!currentConfig.connectors.anthropic) securePrompt("anthropic");
  else await testSavedModel();
  if (legacy) showGmailOauthResult();
  showWorkspaceOauthResult();
}

start();
