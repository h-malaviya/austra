window.renderCapabilityViews = async function renderCapabilityViews(parent, view, departmentId, execution) {
  const capabilities = new Map(view.capabilities.map(item => [item.key, item]));
  for (const draft of view.custom_module_drafts || []) {
    const panel = add(parent, "section", "", "org-panel org-capability-panel");
    add(panel, "h3", draft.key.replaceAll("_", " "));
    add(panel, "p", `Custom module draft for review: ${draft.interaction}`);
    add(panel, "p", `Review ticket ${draft.review_ticket}. Release and rollback follow the normal application process.`);
  }
  await Promise.all(view.projections.map(async descriptor => {
    const panel = add(parent, "section", "", "org-panel org-capability-panel");
    const title = descriptor.title || (descriptor.transition || descriptor.key).replaceAll("_", " ");
    const heading = add(panel, "h3", title);
    panel.setAttribute("aria-label", title);
    const body = add(panel, "div");
    const projectionUrl = `/api/org/capability/${departmentId}/projections/${descriptor.key}`;
    const loadProjection = (parameters = {}) =>
      api(`${projectionUrl}?${new URLSearchParams({ build_id: view.build_id, ...parameters })}`);

    const render = async (restoreFocus = false) => {
      body.replaceChildren();
      const loading = add(body, "p", "Loading view…");
      loading.setAttribute("role", "status");
      try {
        if (descriptor.view === "form") {
          const capability = capabilities.get(descriptor.capability);
          const transition = capability.transitions.find(item => item.key === descriptor.transition);
          const data = transition.from === null ? { records: [] } : await loadProjection();
          renderProjectionForm(body, view, departmentId, capability, transition, data, execution);
        } else {
          const data = await loadProjection();
          if (descriptor.view === "chart" && data.surface && window.AustraA2UI) {
            heading.hidden = true;
            renderOperationalChart(body, data, loadProjection);
          } else {
            renderProjectionTable(body, data, descriptor.fields, loadProjection, title);
          }
        }
        loading.remove();
      } catch (error) {
        heading.hidden = false;
        body.replaceChildren();
        const message = add(body, "p", `Couldn't load this view. ${error.message}`, "org-error");
        message.setAttribute("role", "alert");
        orgButton(body, "Retry", "org-button secondary", () => render(true));
      }
      if (restoreFocus && panel.isConnected && document.activeElement === document.body) {
        const focusTarget = body.querySelector("select:enabled, input:enabled, button:enabled") || panel;
        if (focusTarget === panel) panel.tabIndex = -1;
        focusTarget.focus();
      }
    };
    await render();
  }));
};

function renderProjectionForm(panel, view, departmentId, capability, transition, data, execution) {
  const eligible = data.records.filter(item => item.state === transition.from);
  if (transition.from !== null && !eligible.length) {
    add(panel, "p", data.complete === false
      ? "None of the loaded records are ready for this step."
      : "No records are ready for this step.");
    return;
  }
  if (execution) {
    const fields = transition.fields.map(name => {
      const spec = capability.fields.find(item => item.key === name);
      return {name, label: name.replaceAll("_", " ") + (spec?.unit ? ` (${spec.unit})` : ""),
        type: "text", value: ""};
    });
    if (transition.from !== null) fields.unshift({
      name: "record_id", label: "Record", type: "text",
      value: eligible.length === 1 ? eligible[0].id : "",
      options: eligible.map(item => ({value: item.id, label: item.external_id})),
    });
    const notice = add(panel, "p");
    notice.setAttribute("role", "alert");
    let draft = "";
    let requestKey;
    let correlation;
    workflowForm(panel, transition.key.replaceAll("_", " "), fields, async values => {
      const {record_id, external_id, ...input} = values;
      const current = eligible.find(item => item.id === record_id);
      const payload = {capability_key: capability.key, transition_key: transition.key,
        record_id: record_id || null, external_id: external_id || null, fields: input};
      const serialized = JSON.stringify(payload);
      if (draft !== serialized) {
        draft = serialized;
        requestKey = crypto.randomUUID();
        correlation = crypto.randomUUID();
      }
      try {
        const decision = await api(execution.commandPath, {
          method: "POST", body: JSON.stringify({
            action: "transition_record", department_id: departmentId, build_id: view.build_id,
            idempotency_key: requestKey, correlation_id: correlation, payload,
            expected_revisions: current ? {[current.id]: current.revision} : {},
          })
        });
        execution.complete(decision);
        if (panel.isConnected) {
          panel.replaceChildren();
          add(panel, "p", "Step completed. Choose another step to continue.");
        }
      } catch (error) { notice.textContent = workflowError(error); }
    });
    return;
  }
  const form = add(panel, "form", "", "org-capability-form");
  let recordSelect = null;
  if (transition.from !== null) {
    recordSelect = add(add(form, "label", "Record", "org-capability-field"), "select");
    eligible.forEach(item =>
      capabilityOption(recordSelect, item.id, `${item.external_id} · r${item.revision}`));
  }
  const controls = new Map();
  for (const name of transition.fields) {
    const spec = capability.fields.find(item => item.key === name);
    const fieldLabel = name.replaceAll("_", " ") + (spec?.unit ? ` (${spec.unit})` : "");
    const control = add(add(form, "label", fieldLabel, "org-capability-field"), "input");
    control.required = true;
    control.name = name;
    control.type = spec?.type === "decimal" ? "number" : "text";
    if (spec?.type === "decimal") { control.min = "0"; control.step = "0.000001"; }
    controls.set(name, control);
  }
  const submit = add(form, "button", transition.key.replaceAll("_", " "), "org-button primary");
  submit.type = "submit";
  let previousPayload = "";
  let retryKey = null;
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    const current = eligible.find(item => item.id === recordSelect?.value);
    const fields = Object.fromEntries([...controls].filter(([name]) => name !== "external_id")
      .map(([name, control]) => [name, control.value]));
    const payload = { capability_key: capability.key, transition_key: transition.key,
      record_id: current?.id || null,
      external_id: controls.get("external_id")?.value || null, fields };
    const serialized = JSON.stringify(payload);
    if (previousPayload !== serialized) {
      previousPayload = serialized;
      retryKey = crypto.randomUUID();
    }
    const command = { action: "transition_record", department_id: departmentId,
      idempotency_key: retryKey, correlation_id: crypto.randomUUID(),
      expected_revisions: current ? { [current.id]: current.revision } : {}, payload };
    submit.disabled = true;
    try {
      const decision = await api("/api/org/capability-ui/commands", {
        method: "POST", body: JSON.stringify(command) });
      orgAdminState.lastCapabilityDecision = {decision, departmentId, buildId: view.build_id};
      await window.renderOrganizationManagement(true);
      orgNotice("Command recorded.");
    } catch (error) { orgNotice(error.message, true); }
    finally { submit.disabled = false; }
  };
}

function renderOperationalChart(panel, data, loadProjection) {
  const chart = add(panel, "div", "", "org-workflow-chart");
  const detail = add(panel, "div", "", "org-chart-detail");
  detail.setAttribute("aria-live", "polite");
  let requestId = 0;
  chart.addEventListener("austra-chart-select", async event => {
    const current = ++requestId;
    detail.replaceChildren();
    if (event.detail.key === null) return;
    add(detail, "h3", event.detail.label);
    const status = add(detail, "p", "Loading recordsâ€¦");
    try {
      const selection = await loadProjection({ group_value: event.detail.key });
      if (current !== requestId || !detail.isConnected) return;
      status.remove();
      renderProjectionTable(detail, selection, data.descriptor.fields,
        parameters => loadProjection({ ...parameters, group_value: event.detail.key }), event.detail.label);
    } catch (error) {
      if (current !== requestId || !detail.isConnected) return;
      status.textContent = error.message;
      status.classList.add("org-error");
    }
  });
  window.AustraA2UI.mount(chart, data.surface, () => {});
  if (data.chart_truncated) add(chart, "p", "The chart shows the largest groups.");
}

function renderProjectionTable(panel, data, fields, loadProjection, title) {
  const pageSize = data.records.length;
  if (!pageSize || pageSize >= data.record_count) {
    renderProjectionRows(add(panel, "div"), data, fields, title);
    return;
  }

  const toolbar = add(panel, "div", "", "org-section-head");
  const status = add(toolbar, "p");
  status.setAttribute("role", "status");
  const actions = add(toolbar, "div", "", "org-heading-actions");
  const previous = orgButton(actions, "Previous", "org-button secondary", event =>
    page(Math.max(0, (data.offset ?? 0) - pageSize), event.currentTarget));
  const next = orgButton(actions, "Next", "org-button secondary", event =>
    page((data.offset ?? 0) + pageSize, event.currentTarget));
  const errorMessage = add(panel, "p", "", "org-error");
  errorMessage.setAttribute("role", "alert");
  errorMessage.hidden = true;
  const rows = add(panel, "div");
  renderProjectionRows(rows, data, fields, title);
  let loading = false;
  updatePagination();

  function updatePagination() {
    const offset = data.offset ?? 0;
    const last = offset + data.records.length;
    const range = data.records.length > 1 ? `${offset + 1}–${last}` : String(last);
    previous.disabled = loading || offset === 0;
    next.disabled = loading || !data.records.length || last >= data.record_count;
    status.textContent = loading ? "Loading records…"
      : data.records.length ? `${range} of ${data.record_count} records`
        : `No records on this page. ${data.record_count} available.`;
  }

  async function page(offset, trigger) {
    if (loading) return;
    const restoreFocus = document.activeElement === trigger;
    loading = true;
    errorMessage.hidden = true;
    updatePagination();
    try {
      const result = await loadProjection({ offset });
      if (!rows.isConnected) return;
      const replacement = document.createDocumentFragment();
      renderProjectionRows(replacement, result, fields, title);
      rows.replaceChildren(replacement);
      data = result;
    } catch (error) {
      errorMessage.hidden = false;
      errorMessage.textContent = `Couldn't load these records. ${error.message}`;
    } finally {
      loading = false;
      updatePagination();
      if (restoreFocus && rows.isConnected
        && (document.activeElement === document.body || document.activeElement === trigger)) {
        const focusTarget = [trigger, previous, next].find(button => !button.disabled) || status;
        if (focusTarget === status) status.tabIndex = -1;
        focusTarget.focus();
      }
    }
  }
}

function renderProjectionRows(panel, data, fields, title) {
  panel.replaceChildren();
  if (data.excluded_demo_count || data.excluded_hold_count) add(panel, "p",
    `${data.excluded_demo_count} demo and ${data.excluded_hold_count} held or unverified records excluded.`);
  if (data.total !== undefined) {
    add(panel, "p", data.total === null ? `${data.unresolved_count} unresolved quantities`
      : `${data.total} ${data.unit}`);
  }
  if (!data.records.length) {
    add(panel, "p", "No matching records.");
    return;
  }
  const scroll = add(panel, "div", "", "org-table-scroll");
  const table = add(scroll, "table", "", "org-table");
  table.setAttribute("aria-label", title);
  const headings = add(add(table, "thead"), "tr");
  fields.forEach(field => add(headings, "th", field.replaceAll("_", " ")).scope = "col");
  const body = add(table, "tbody");
  data.records.forEach(record => {
    const row = add(body, "tr");
    fields.forEach(field => add(row, "td", String(record.values[field] ?? "Unknown")));
  });
}
