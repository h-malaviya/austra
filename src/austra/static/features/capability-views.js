window.renderCapabilityViews = async function renderCapabilityViews(parent, view, departmentId) {
  const capabilities = new Map(view.capabilities.map(item => [item.key, item]));
  for (const draft of view.custom_module_drafts || []) {
    const panel = add(parent, "section", "", "org-panel org-capability-panel");
    add(panel, "h3", draft.key.replaceAll("_", " "));
    add(panel, "p", `Custom module draft for review: ${draft.interaction}`);
    add(panel, "p", `Review ticket ${draft.review_ticket}. Release and rollback follow the normal application process.`);
  }
  for (const descriptor of view.projections) {
    const panel = add(parent, "section", "", "org-panel org-capability-panel");
    add(panel, "h3", descriptor.key.replaceAll("_", " "));
    if (descriptor.view === "form") {
      const capability = capabilities.get(descriptor.capability);
      const transition = capability.transitions.find(item => item.key === descriptor.transition);
      const form = add(panel, "form", "", "org-capability-form");
      const recordData = transition.from === null ? { records: [] }
        : await api(`/api/org/capability/${departmentId}/projections/${descriptor.key}`);
      let recordSelect = null;
      if (transition.from !== null) {
        recordSelect = add(add(form, "label", "Record", "org-capability-field"), "select");
        recordData.records.filter(item => item.state === transition.from).forEach(item =>
          capabilityOption(recordSelect, item.id, `${item.external_id} · r${item.revision}`));
      }
      const controls = new Map();
      for (const name of transition.fields) {
        const spec = capability.fields.find(item => item.key === name);
        const control = add(add(form, "label", name.replaceAll("_", " "), "org-capability-field"), "input");
        control.required = true;
        control.name = name;
        control.type = spec?.type === "decimal" ? "number" : "text";
        if (spec?.type === "decimal") { control.min = "0"; control.step = "0.000001"; }
        controls.set(name, control);
      }
      const submit = add(form, "button", transition.key.replaceAll("_", " "), "org-button primary");
      submit.type = "submit";
      let retryKey = null;
      form.onsubmit = async event => {
        event.preventDefault();
        const current = recordData.records.find(item => item.id === recordSelect?.value);
        const fields = Object.fromEntries([...controls].filter(([name]) => name !== "external_id")
          .map(([name, control]) => [name, control.value]));
        const payload = { capability_key: capability.key, transition_key: transition.key,
          record_id: current?.id || null,
          external_id: controls.get("external_id")?.value || null, fields };
        const command = { action: "transition_record", department_id: departmentId,
          idempotency_key: retryKey ||= crypto.randomUUID(), correlation_id: crypto.randomUUID(),
          expected_revisions: current ? { [current.id]: current.revision } : {}, payload };
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
      continue;
    }
    const data = await api(`/api/org/capability/${departmentId}/projections/${descriptor.key}`);
    if (data.excluded_demo_count || data.excluded_hold_count) add(panel, "p",
      `${data.excluded_demo_count} demo and ${data.excluded_hold_count} held or unverified records excluded.`);
    if (data.total !== undefined) {
      add(panel, "p", data.total === null ? `${data.unresolved_count} unresolved quantities`
        : `${data.total} ${data.unit}`);
    }
    if (!data.complete) add(panel, "p", `Showing ${data.records.length} of ${data.record_count} records. Use the API for the complete set.`);
    const table = add(panel, "table", "", "org-table");
    const headings = add(table, "tr");
    descriptor.fields.forEach(field => add(headings, "th", field.replaceAll("_", " ")));
    data.records.forEach(record => {
      const row = add(table, "tr");
      descriptor.fields.forEach(field => add(row, "td", String(record.values[field] ?? "Unknown")));
      if (descriptor.view === "chart" && data.total !== null && descriptor.measure) {
        const bar = add(row, "td", "", "org-chart-bar");
        const total = Number(data.total);
        const value = Number(record.values[descriptor.measure]);
        bar.style.width = `${total > 0 && Number.isFinite(value) ? Math.min(100, 100 * value / total) : 0}%`;
      }
    });
  }
};
