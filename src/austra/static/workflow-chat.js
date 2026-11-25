const capabilityFields = {
  submit_order: [["external_id", "Order ID"], ["product", "Product"],
    ["quantity_kg", "Quantity (kg)"]],
  reserve_stock: [["order_id", "Sales order", "sales_order"],
    ["lot_id", "Stock lot", "stock_lot"], ["quantity_kg", "Quantity (kg)"]],
  draft_po: [["order_id", "Sales order", "sales_order"], ["external_id", "PO ID"],
    ["supplier", "Supplier"], ["quantity_kg", "Quantity (kg)"],
    ["unit_price_per_kg", "Price per kg"], ["currency", "Currency (ISO 4217)"]],
  approve_po: [["po_id", "Purchase order", "purchase_order"]],
  propose_email: [["po_id", "Purchase order", "purchase_order"],
    ["external_id", "Proposal ID"], ["recipient", "Recipient"],
    ["subject", "Subject"], ["body", "Message"]],
  approve_email: [["email_id", "Email proposal", "email_proposal"]]
};

function wantsWorkflowChat(text) {
  return /^(?:please\s+)?(?:run|start|open|continue|execute|use)\b.{0,140}\bworkflow\b/i.test(text)
    && !/^.{0,25}\b(?:draft|create|build|design)\b/i.test(text);
}

const workflowNext = {
  submit_order: "reserve_stock", reserve_stock: "draft_po", draft_po: "approve_po",
  approve_po: "propose_email", propose_email: "approve_email", approve_email: "email_jobs"
};

function workflowForm(panel, title, fields, submit) {
  const id = "workflow_input";
  const mountInput = add(panel, "div");
  if (window.AustraA2UI) {
    window.AustraA2UI.mount(mountInput, {id, messages: [
      {version: "v0.9.1", createSurface: {surfaceId: id, catalogId: "https://austra.local/catalogs/report.json"}},
      {version: "v0.9.1", updateComponents: {surfaceId: id, components: [
        {id: "root", component: "AustraWorkflowInput", title, label: title, fields}
      ]}}
    ]}, () => {});
    mountInput.addEventListener("austra-workflow-submit", async event => {
      try { await submit(event.detail.values); }
      finally { event.detail.done(); }
    });
    return;
  }
  const form = add(mountInput, "form", "", "org-capability-form");
  form.setAttribute("aria-label", title);
  for (const field of fields) {
    const label = add(form, "label", field.label, "org-capability-field");
    const control = add(label, field.options ? "select" : field.type === "textarea" ? "textarea" : "input");
    if (field.options) {
      capabilityOption(control, "", "Choose a record");
      field.options.forEach(option => capabilityOption(control, option.value, option.label));
    } else if (field.type !== "textarea") control.type = field.type;
    control.name = field.name;
    control.required = true;
    control.value = field.value;
  }
  const button = add(form, "button", title, "org-button primary");
  button.type = "submit";
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    try { await submit(Object.fromEntries(new FormData(form))); }
    finally { button.disabled = false; }
  };
}

function purchaseRecords(step, kind, records) {
  const filter = {
    reserve_stock: {sales_order: ["open", "partially_reserved"], stock_lot: ["available"]},
    draft_po: {sales_order: ["open", "partially_reserved"]},
    approve_po: {purchase_order: ["draft"]},
    propose_email: {purchase_order: ["approved"]},
    approve_email: {email_proposal: ["proposed"]}
  }[step]?.[kind];
  return records.filter(record => record.kind === kind && (!filter || filter.includes(record.state)));
}

function workflowError(error) {
  const message = error.message || "Couldn’t complete this step.";
  if (/workflows\.approve denied|designated workflow reviewer/i.test(message)) {
    return "This step requires an authorized workflow approver.";
  }
  if (/revision changed|stale|source .*changed/i.test(message)) {
    return `${message} Reopen this step to review the latest record before retrying.`;
  }
  return message;
}

function workflowStepLabel(descriptor) {
  return descriptor.title || workflowLabel(descriptor.transition || descriptor.key);
}

function authorizeWorkflowGmail(parent, report, resume, legacy, connectionId) {
  parent.replaceChildren();
  add(parent, "p", "Authorize Gmail to let Austra read matching reply text and attachments on this workflow.");
  orgButton(parent, "Connect Gmail with Google", "org-button secondary", async () => {
    const popup = window.open("about:blank", "_blank");
    if (!popup) { report("Allow the Google authorization tab and try again.", true); return; }
    popup.opener = null;
    try {
      const path = legacy ? "/api/connections/gmail/start?read_content=true" : "/api/org/connections/gmail/start";
      const result = await api(path, {method: "POST",
        body: JSON.stringify({read_content: true, connection_id: connectionId || null})});
      const url = new URL(result.authorization_url);
      if (url.origin !== "https://accounts.google.com" || url.pathname !== "/o/oauth2/v2/auth") {
        throw new Error("Austra couldn’t open Google authorization.");
      }
      popup.location.replace(url.href);
      report("Finish Google authorization, then return to this chat.");
    } catch (error) { popup.close(); report(workflowError(error), true); }
  });
  orgButton(parent, "Use authorized Gmail", "org-button secondary", async () => {
    try {
      if (legacy) {
        await api("/api/connections/gmail/link-workflow", {method: "POST"});
      }
      await resume();
    } catch (error) { report(workflowError(error), true); }
  });
}

function mountWorkflowConversation(chat, initialText = "", {isCurrent, legacy}) {
  const card = add(bubble(""), "section", "", "workflow-chat-card");
  const heading = add(card, "h3", "Run a released workflow");
  const controls = add(card, "div", "", "workflow-chat-controls");
  const notice = add(card, "p", "", "workflow-chat-notice");
  notice.setAttribute("role", "status");
  const graph = add(card, "div");
  const outcome = add(card, "div");
  const actionPanel = add(card, "div");
  let catalog = [];
  let selection = chat.workflow || {};
  let view = null;
  let version = 0;
  let submitting = false;
  const picked = {};

  function report(message, error = false) {
    notice.textContent = message;
    notice.classList.toggle("org-error", error);
  }

  const viewUrl = () => `/api/org/capability/${selection.department_id}?build_id=${selection.build_id}`;
  const save = () => api(`/api/org/workflow-chats/${chat.id}`, {
    method: "PATCH", body: JSON.stringify(selection)
  });
  const active = () => card.isConnected && isCurrent();

  function choices(message) {
    version += 1;
    graph.replaceChildren();
    outcome.replaceChildren();
    actionPanel.replaceChildren();
    controls.replaceChildren();
    if (!catalog.length) { report("I couldn’t find a released workflow you can access."); return; }
    report(message);
    for (const item of catalog) {
      orgButton(controls, `${item.name} · ${item.department}`, "org-button secondary", () => choose(item));
    }
  }

  async function choose(item) {
    selection = {department_id: item.department_id, build_id: item.build_id, step: null, email_reference: null};
    try { await selectStep(null); }
    catch (error) { report(workflowError(error), true); }
  }

  async function selectStep(step, { refresh = true, persist = true } = {}) {
    const current = ++version;
    const updated = refresh ? await api(viewUrl()) : view;
    if (!active() || current !== version) return;
    view = updated;
    selection.step = step || (view.capabilities ? view.projections[0]?.key : view.commands.includes("submit_order")
      ? "submit_order" : view.commands[0]) || null;
    if (persist) await save();
    if (!active() || current !== version) return;
    showStep();
  }

  function openStep(step) {
    if (!submitting) return selectStep(step).catch(error => report(workflowError(error), true));
  }

  function showRecordEmail() {
    if (view.email_records.length) {
      const options = view.email_records.map(record => ({
        value: record.id, label: `${record.external_id} · ${workflowLabel(record.state)}`
      }));
      const fields = [
        {name: "record_id", label: "Which record should this email track?", type: "text",
          options, value: options.length === 1 ? options[0].value : ""},
        {name: "recipient", label: "Recipient", type: "email", value: ""},
        {name: "subject", label: "Subject", type: "text", value: ""},
        {name: "body", label: "Message", type: "textarea", value: ""},
      ];
      let previous;
      let requestKey;
      workflowForm(actionPanel, "Prepare email for review", fields, async values => {
        if (!active() || submitting) return;
        const snapshot = JSON.stringify(values);
        if (snapshot !== previous) {
          previous = snapshot;
          requestKey = crypto.randomUUID();
        }
        submitting = true;
        try {
          const record = view.email_records.find(item => item.id === values.record_id);
          const result = await api(`/api/org/workflow-email/${selection.department_id}/${selection.build_id}`, {
            method: "POST", body: JSON.stringify({...values, expected_revision: record.revision, idempotency_key: requestKey})
          });
          if (!active()) return;
          selection.email_reference = result.reference;
          await selectStep("email_jobs");
        } catch (error) { report(workflowError(error), true); }
        finally { submitting = false; }
      });
    } else if (!view.email_jobs.length) {
      report("Create a record in an approved state before preparing an email.");
    }
    if (view.email_jobs.length) showEmailJobs();
  }

  function showEmailJobs() {
    renderCapabilityEmailJobs(actionPanel, {...view, email_jobs: view.email_jobs || []}, selection.department_id, {
      report, emailReference: selection.email_reference,
      connectGmail: (parent, connectionId) => authorizeWorkflowGmail(
        parent, report, () => selectStep("email_jobs"), legacy, connectionId,
      ),
    });
  }

  function showStep() {
    heading.textContent = view.name || "Released workflow";
    controls.replaceChildren();
    orgButton(controls, "Change workflow", "org-button secondary", () => {
      if (!submitting) choices("Which workflow should I open?");
    });
    graph.replaceChildren();
    actionPanel.replaceChildren();
    report("");
    if (view.capabilities) {
      const descriptor = view.projections.find(item => item.key === selection.step);
      for (const item of view.projections) {
        orgButton(graph, workflowStepLabel(item), "org-button secondary", () => openStep(item.key));
      }
      if (view.email_records) {
        orgButton(graph, workflowLabel("email_jobs"), "org-button secondary", () => openStep("email_jobs"));
        if (selection.step === "email_jobs") showRecordEmail();
      }
      if (descriptor) window.renderCapabilityViews(actionPanel, {...view, projections: [descriptor]}, selection.department_id, {
        commandPath: "/api/org/capability-chat/commands",
        complete: decision => capabilityResult(outcome, decision),
      }).catch(error => report(workflowError(error), true));
      return;
    }
    purchaseWorkflowGraph(graph, view, openStep, selection.step);
    const step = selection.step;
    if (step === "email_jobs") {
      showEmailJobs();
      return;
    }
    const definitions = capabilityFields[step];
    if (!definitions) { report("Which step should I open? Choose a step in the workflow."); return; }
    const fields = definitions.map(([name, label, kind]) => {
      const eligible = kind ? purchaseRecords(step, kind, view.records) : null;
      const field = {name, label, type: name === "body" ? "textarea" : name === "recipient" ? "email" : "text",
        value: name === "currency" ? "USD" : ""};
      if (eligible) {
        field.options = eligible.map(record => ({value: record.id, label: `${record.external_id} · ${record.state.replaceAll("_", " ")}`}));
        field.value = eligible.find(record => record.id === picked[kind])?.id || (eligible.length === 1 ? eligible[0].id : "");
      }
      return field;
    });
    const missing = fields.find(field => field.options && !field.options.length);
    if (missing) { report(`I need an eligible ${missing.label.toLowerCase()} before this step. Review the previous step.`, true); return; }
    if (step === "approve_email" || step === "approve_po") {
      if (fields[0].options.length > 1 && !fields[0].value) {
        report("Which record should I bring up for approval?");
        for (const option of fields[0].options) {
          orgButton(actionPanel, option.label, "org-button secondary", () => {
            if (!submitting) {
              picked[definitions[0][2]] = option.value;
              showStep();
            }
          });
        }
        return;
      }
      fields[0].options = fields[0].options.filter(option => option.value === fields[0].value);
      const source = view.records.find(record => record.id === fields[0].value);
      if (source && step === "approve_email") {
        add(actionPanel, "p", `To ${source.data.recipient}`);
        add(actionPanel, "strong", source.data.subject);
        add(actionPanel, "pre", source.data.body);
      }
      if (source && step === "approve_po") {
        add(actionPanel, "p", `${source.data.quantity_kg} kg · ${source.data.supplier} · ${source.data.currency} ${source.data.total}`);
      }
      const rule = view.rules?.find(item => item.decision_key === `command.${step}`);
      if (rule?.limit != null) add(actionPanel, "p", `Approval limit ${rule.unit} ${rule.limit}. Higher amounts escalate.`);
    }
    let previous = "";
    let requestKey;
    let correlation;
    workflowForm(actionPanel, workflowLabel(step), fields, async payload => {
      if (!active() || submitting) return;
      const snapshot = JSON.stringify(payload);
      if (snapshot !== previous) {
        previous = snapshot;
        requestKey = crypto.randomUUID();
        correlation = crypto.randomUUID();
      }
      submitting = true;
      try {
        const command = {action: step, department_id: selection.department_id, build_id: selection.build_id,
          idempotency_key: requestKey, correlation_id: correlation,
          expected_revisions: capabilityRevisions(payload, view.records), payload};
        const decision = await api("/api/org/capability-chat/commands", {
          method: "POST", body: JSON.stringify(command)
        });
        if (!active()) return;
        for (const record of [decision.record, decision.order, decision.lot].filter(Boolean)) picked[record.kind] = record.id;
        capabilityResult(outcome, decision);
        if (decision.escalation_required) {
          actionPanel.replaceChildren();
          report("The purchase exceeded the approval limit and was escalated. The email cannot be drafted yet.", true);
          return;
        }
        if (step === "approve_email") selection.email_reference = decision.record.external_id;
        await selectStep(workflowNext[step]);
      } catch (error) { report(workflowError(error), true); }
      finally { submitting = false; }
    });
  }

  async function respond(text) {
    if (!active()) return;
    if (submitting) { report("Wait for the current action to finish before changing steps."); return; }
    const words = value => value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    const normalized = words(text);
    const named = catalog.filter(item => normalized.includes(words(item.name)));
    const scoped = named.filter(item => normalized.includes(words(item.department)));
    const matches = scoped.length ? scoped : named;
    if (matches.length === 1) { await choose(matches[0]); return; }
    if (!view || matches.length > 1 || /change workflow|which workflow|another workflow/i.test(text)) {
      if (catalog.length === 1 && /^(?:please )?(?:run|start|open|continue|execute|use) (?:a |the )?workflow$/.test(normalized)) await choose(catalog[0]);
      else choices("Which released workflow and department should I use?");
      return;
    }
    const steps = view.capabilities ? view.projections.map(item => [item.key, workflowStepLabel(item)])
      : [...view.commands, "email_jobs"].map(key => [key, workflowLabel(key)]);
    if (view.email_records) steps.push(["email_jobs", workflowLabel("email_jobs")]);
    const requested = steps.filter(([key, label]) => normalized.includes(label.toLowerCase())
      || (key === "email_jobs" && /\b(?:send|prepare|track)\b.*\b(?:email|replies)\b/i.test(normalized)));
    if (requested.length === 1) { await openStep(requested[0][0]); return; }
    report(requested.length > 1 ? "Which of those steps should I open first?"
      : "Which step should I open? For the current step, use the fields and approval shown below.");
  }

  api("/api/org/released-workflows").then(async available => {
    if (!active()) return;
    catalog = Array.isArray(available) ? available : [];
    if (selection.build_id) await selectStep(selection.step, {persist: false});
    else if (initialText) await respond(initialText);
    else choices("Which released workflow and department should I use?");
  }).catch(error => { if (active()) report(workflowError(error), true); });
  return {active, respond};
}
