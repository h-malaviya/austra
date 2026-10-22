async function sendMessage() {
  if (busy) return;
  const input = byId("messageInput");
  const text = input.value.trim();
  if (!text && !pendingFile) return;
  busy = true;
  byId("sendButton").disabled = true;
  try {
    if (guided) {
      await handleGuided(text);
      input.value = "";
      resizeComposer();
      return;
    }
    if (pendingFile) manager();
    const sender = byId("managerName").value.trim() || "Owner";
    if (text || !pendingFile || fileMessageSavedForCaseId !== selectedCaseId) {
      const message = text || `Please review the attached file: ${pendingFile.name}`;
      if (!selectedCaseId) {
        const created = await api("/api/cases", { method: "POST", body: JSON.stringify({ message, sender }) });
        selectedCaseId = created.id;
      } else {
        await api(`/api/cases/${selectedCaseId}/messages`, {
          method: "POST", body: JSON.stringify({ message, sender })
        });
      }
      if (pendingFile) fileMessageSavedForCaseId = selectedCaseId;
    }
    input.value = "";
    resizeComposer();
    if (pendingFile) { await uploadFile(selectedCaseId, pendingFile); resetAttachment(); }
    if (modelConfigured) await analyzeChat(selectedCaseId);
    else status("Chat saved. Connect the Anthropic API key for agent replies.");
    await showChat(selectedCaseId);
  } catch (error) {
    status(error.message, true);
    if (selectedCaseId) await showChat(selectedCaseId);
  } finally {
    busy = false;
    byId("sendButton").disabled = false;
    input.focus();
  }
}

async function showPolicies() {
  try {
    const policies = await api("/api/policies");
    const list = byId("policyList");
    list.replaceChildren();
    if (!policies.length) add(list, "p", "No policies yet.");
    policies.forEach(policy => {
      const card = add(list, "div", "", "review-card");
      const title = add(card, "strong", `${policy.title} · v${policy.version}`);
      add(title, "span", policy.status, "badge");
      add(card, "p", policy.body);
      if (policy.status === "draft") {
        const activate = add(card, "button", "Activate policy", "inline-button");
        activate.type = "button";
        activate.onclick = async () => {
          try {
            const access = manager();
            await api(`/api/policies/${policy.id}/activate`, {
              method: "POST", headers: access.headers,
              body: JSON.stringify({ decision: "approved", manager: access.name })
            });
            status("Policy activated.");
            await showPolicies();
          } catch (error) { status(error.message, true); }
        };
      }
    });
    openDialog("policiesDialog");
  } catch (error) { status(error.message, true); }
}

function startPolicyDraft() {
  try {
    manager();
    closeDialog("policiesDialog");
    guided = { kind: "policy", step: "title" };
    localNote("Let's draft a policy. What should it be called? Type the title below. You can type ‘cancel’ to stop.");
    byId("messageInput").focus();
  } catch (error) { status(error.message, true); }
}

async function startRecordEntry() {
  try {
    if (!selectedCaseId) throw new Error("Open a chat first to add case data.");
    manager();
    definitions = await api("/api/definitions");
    const active = definitions.filter(entry => entry.status === "active" && entry.fields.length);
    if (!active.length) throw new Error("No approved record types are available yet.");
    const bubble = assistantBubble("Which type of case data would you like to add?");
    const controls = add(bubble, "div", "", "card-actions");
    active.forEach(definition => {
      const button = add(controls, "button", definition.name);
      button.type = "button";
      button.onclick = () => {
        guided = { kind: "record", definition, index: 0, values: {} };
        localNote(`What is the ${definition.fields[0].name.replaceAll("_", " ")}? Type ‘skip’ if unknown, or ‘cancel’ to stop.`);
        byId("messageInput").focus();
      };
    });
    scrollBottom();
  } catch (error) { status(error.message, true); }
}

function parseRecordValue(field, text) {
  if (field.data_type === "number") {
    const number = Number(text);
    if (!Number.isFinite(number)) throw new Error("Please enter a number, or type ‘skip’.");
    return number;
  }
  if (field.data_type === "boolean") {
    if (!["yes", "no", "true", "false"].includes(text.toLowerCase())) {
      throw new Error("Please answer yes or no, or type ‘skip’.");
    }
    return ["yes", "true"].includes(text.toLowerCase());
  }
  if (field.data_type === "date" && !/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    throw new Error("Please use YYYY-MM-DD for the date, or type ‘skip’.");
  }
  return text;
}

async function handleGuided(text) {
  if (!text) throw new Error("Write a reply to continue, or type ‘cancel’.");
  localUser(text);
  if (text.toLowerCase() === "cancel") { guided = null; localNote("Cancelled."); return; }
  if (guided.kind === "policy") {
    if (guided.step === "title") {
      if (text.length < 3 || text.length > 200) throw new Error("Use a title between 3 and 200 characters.");
      guided.title = text;
      guided.step = "body";
      localNote("Now describe the rule, including when it applies and who may approve it.");
      return;
    }
    const access = manager();
    await api("/api/policies", {
      method: "POST", headers: access.headers,
      body: JSON.stringify({ title: guided.title, body: text })
    });
    guided = null;
    localNote("Policy draft saved. Open Policies to review and activate it.");
    return;
  }
  const field = guided.definition.fields[guided.index];
  if (text.toLowerCase() !== "skip") guided.values[field.name] = parseRecordValue(field, text);
  guided.index += 1;
  if (guided.index < guided.definition.fields.length) {
    const next = guided.definition.fields[guided.index];
    localNote(`What is the ${next.name.replaceAll("_", " ")}? Type ‘skip’ if unknown.`);
    return;
  }
  const access = manager();
  await api(`/api/cases/${selectedCaseId}/records`, {
    method: "POST", headers: access.headers,
    body: JSON.stringify({ definition_id: guided.definition.id, values: guided.values, actor: access.name })
  });
  guided = null;
  status("Case data saved as unverified.");
  await showChat(selectedCaseId);
}

byId("newChat").onclick = () => {
  guided = null;
  resetAttachment();
  byId("messageInput").value = "";
  resizeComposer();
  status("");
  renderWelcome();
  loadChats().catch(error => status(error.message, true));
  byId("sidebar").classList.remove("open");
  byId("messageInput").focus();
};
byId("sendButton").onclick = sendMessage;
byId("messageInput").oninput = resizeComposer;
byId("messageInput").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
};
byId("attachButton").onclick = () => byId("fileInput").click();
byId("fileInput").onchange = () => {
  pendingFile = byId("fileInput").files[0] || null;
  fileMessageSavedForCaseId = null;
  const preview = byId("attachmentPreview");
  preview.replaceChildren();
  preview.hidden = !pendingFile;
  if (pendingFile) {
    add(preview, "span", `📎 ${pendingFile.name}`);
    const remove = add(preview, "button", "Remove");
    remove.type = "button";
    remove.onclick = resetAttachment;
  }
};
byId("accessButton").onclick = () => openDialog("accessDialog");
byId("closeAccess").onclick = () => closeDialog("accessDialog");
byId("saveAccess").onclick = () => {
  try { manager(); closeDialog("accessDialog"); status("Manager access ready for this tab."); }
  catch (error) { status(error.message, true); }
};
byId("policiesButton").onclick = showPolicies;
byId("policyTopButton").onclick = showPolicies;
byId("closePolicies").onclick = () => closeDialog("policiesDialog");
byId("draftPolicy").onclick = startPolicyDraft;
byId("recordsButton").onclick = startRecordEntry;
byId("menuButton").onclick = () => byId("sidebar").classList.toggle("open");
document.querySelectorAll(".dialog-backdrop").forEach(dialog => {
  dialog.onclick = event => { if (event.target === dialog) dialog.hidden = true; };
});

async function start() {
  renderWelcome();
  try {
    const config = await api("/api/config");
    modelConfigured = config.model_configured;
    managerConfigured = config.manager_configured;
    gmailConfigured = config.gmail_configured;
    definitions = await api("/api/definitions");
    await loadChats();
    const connection = byId("connection");
    connection.textContent = modelConfigured ? "Austra is ready" : "Agent key not connected";
    connection.classList.toggle("ready", modelConfigured);
    if (!modelConfigured) status("Chats are available. Add ANTHROPIC_API_KEY on the server for agent replies.");
  } catch (error) { status(error.message, true); }
}
start();
