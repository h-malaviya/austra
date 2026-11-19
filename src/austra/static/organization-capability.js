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

function capabilityOption(select, value, label) {
  const option = add(select, "option", label);
  option.value = value;
}

function capabilityInput(parent, field, records) {
  const [name, label, recordKind] = field;
  const wrapper = add(parent, "label", label, "org-capability-field");
  let control;
  if (recordKind) {
    control = add(wrapper, "select");
    capabilityOption(control, "", "Choose a record");
    records.filter(record => record.kind === recordKind).forEach(record =>
      capabilityOption(control, record.id, `${record.external_id} · ${record.state} · r${record.revision}`));
  } else if (name === "body") {
    control = add(wrapper, "textarea", "", "org-onboarding-textarea");
  } else {
    control = add(wrapper, "input");
    control.type = name === "recipient" ? "email" : "text";
    if (name === "currency") control.value = "USD";
  }
  control.name = name;
  control.required = true;
  control.setAttribute("aria-label", label);
  return control;
}

function capabilityRevisions(payload, records) {
  const revisions = {};
  const byId = new Map(records.map(record => [record.id, record]));
  const include = id => {
    const record = byId.get(id);
    if (!record) throw new Error("Choose a current source record.");
    revisions[id] = record.revision;
    return record;
  };
  if (payload.order_id) include(payload.order_id);
  if (payload.lot_id) include(payload.lot_id);
  if (payload.po_id) {
    const po = include(payload.po_id);
    if (po.data.order_id && !payload.subject) include(po.data.order_id);
  }
  if (payload.email_id) {
    const email = include(payload.email_id);
    include(email.data.po_id);
  }
  return revisions;
}

function capabilityResult(parent, decision) {
  parent.replaceChildren();
  const result = add(parent, "div", "", "org-panel org-capability-result");
  add(result, "h3", "Decision recorded");
  for (const key of ["record", "order", "lot"]) {
    const record = decision[key];
    if (record) add(result, "p", `${record.external_id}: ${record.state} (revision ${record.revision})`);
  }
  if (decision.preview) {
    add(result, "p", `To: ${decision.preview.recipient}`);
    add(result, "p", `Subject: ${decision.preview.subject}`);
    add(result, "pre", decision.preview.body);
    add(result, "p", "Approval required before sending.");
  }
  if (decision.escalation_required) add(result, "p", "Above approval limit. Escalation required.");
  if (decision.effect === "repair_connection") add(result, "p", "Reconnect the email provider to send.");
  add(result, "small", `Blueprint ${decision.blueprint_id} · Build ${decision.build_id} · Event ${decision.event_id}`);
}

function capabilityForm(content, view, departmentId) {
  const panel = add(content, "section", "", "org-panel org-capability-panel");
  add(panel, "h2", "Run a command");
  const form = add(panel, "form", "", "org-capability-form");
  const actionLabel = add(form, "label", "Action", "org-capability-field");
  const action = add(actionLabel, "select");
  view.commands.forEach(name => capabilityOption(action, name, name.replaceAll("_", " ")));
  const fields = add(form, "div", "", "org-capability-fields");
  add(content, "section", "", "org-capability-output");
  let retryKey = null;
  function renderFields() {
    fields.replaceChildren();
    (capabilityFields[action.value] || []).forEach(field => capabilityInput(fields, field, view.records));
    retryKey = null;
  }
  action.onchange = renderFields;
  renderFields();
  const submit = add(form, "button", "Run command", "org-button primary");
  submit.type = "submit";
  form.onsubmit = async event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form).entries());
    delete payload.action;
    const command = { action: action.value, department_id: departmentId,
      idempotency_key: retryKey ||= crypto.randomUUID(), correlation_id: crypto.randomUUID(),
      expected_revisions: capabilityRevisions(payload, view.records), payload };
    submit.disabled = true;
    try {
      const decision = await api("/api/org/capability-ui/commands", {
        method: "POST", body: JSON.stringify(command) });
      orgAdminState.lastCapabilityDecision = decision;
      await window.renderOrganizationManagement(true);
      orgNotice("Command recorded.");
    } catch (error) { orgNotice(error.message, true); }
    finally { submit.disabled = false; }
  };
}

async function orgCapabilityPage(content) {
  const departments = (await api("/api/org/departments")).filter(item => !item.archived);
  const heading = add(content, "div", "", "org-page-heading");
  add(heading, "h2", "Released workflow");
  if (!departments.length) {
    add(content, "p", "Create a department before running a workflow.");
    return;
  }
  const department = add(heading, "select");
  department.setAttribute("aria-label", "Workflow department");
  departments.forEach(item => capabilityOption(department, item.id, item.name));
  const selection = new URLSearchParams(window.location.search).get("department");
  if (departments.some(item => item.id === selection)) department.value = selection;
  department.onchange = () => {
    window.history.replaceState({}, "", `${orgCapabilityPath}?department=${department.value}`);
    window.renderOrganizationManagement().catch(error => orgNotice(error.message, true));
  };
  let view;
  try { view = await api(`/api/org/capability/${department.value}`); }
  catch (error) {
    add(content, "p", error.message.includes("released capability")
      ? "No approved workflow has been released for this organization yet." : error.message);
    return;
  }
  if (view.capabilities) {
    await window.renderCapabilityViews(content, view, department.value);
    if (orgAdminState.lastCapabilityDecision) {
      const result = add(content, "section", "", "org-capability-output");
      capabilityResult(result, orgAdminState.lastCapabilityDecision);
    }
    return;
  }
  if (!view.projections.some(item => item.key === "journey")) {
    add(content, "p", "The released blueprint has no workflow projection.");
    return;
  }
  const summary = add(content, "section", "", "org-panel org-capability-panel");
  add(summary, "h3", "Current records");
  add(summary, "p", "Chat accepts /capability followed by the same typed command JSON.");
  if (!view.complete) add(summary, "p", `Showing ${view.records.length} of ${view.record_count} records. Refresh or use the API for the full set.`);
  if (!view.records.length) add(summary, "p", "No sales orders or stock lots are present yet.");
  else view.records.forEach(record => add(summary, "p",
    `${record.kind.replaceAll("_", " ")}: ${record.external_id} · ${record.state} · r${record.revision}`));
  capabilityForm(content, view, department.value);
  if (orgAdminState.lastCapabilityDecision) {
    capabilityResult(content.querySelector(".org-capability-output"),
      orgAdminState.lastCapabilityDecision);
  }
}
