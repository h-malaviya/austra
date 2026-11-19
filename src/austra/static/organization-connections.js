function connectionField(parent, label, name, secret = false) {
  const wrapper = add(parent, "label", label, "org-capability-field");
  const input = add(wrapper, "input");
  input.name = name;
  input.type = secret ? "password" : "text";
  input.autocomplete = "off";
  input.required = true;
  return input;
}

function connectionActions(parent, connection) {
  const actions = add(parent, "div", "", "org-source-row");
  for (const action of connection.actions) {
    const row = add(actions, "div", "", "org-source-row");
    add(row, "strong", `${action.name} · ${action.effect === "unreviewed" ? "Needs review" : action.effect}`);
    add(row, "small", `Schema ${action.schema_hash.slice(0, 12)}…`);
    if (action.effect !== "unreviewed") continue;
    const effect = add(row, "select");
    for (const value of ["read", "write"]) {
      const option = add(effect, "option", value === "read" ? "Reads data" : "Changes data");
      option.value = value;
    }
    const receipt = connectionField(row, "Provider receipt field for changes", "receipt_field");
    receipt.required = false;
    const scopes = connectionField(row, "Required scopes, comma separated", "scopes");
    scopes.required = false;
    orgButton(row, "Review action", "org-button secondary", async () => {
      try {
        await api(`/api/org/connections/${connection.id}/review-action`, { method: "POST",
          body: JSON.stringify({ action_name: action.name, effect: effect.value,
            receipt_field: receipt.value.trim(),
            scopes: scopes.value.split(",").map(value => value.trim()).filter(Boolean) }) });
        await window.renderOrganizationManagement(true);
        orgNotice("Action classified. A released workflow still needs explicit approval before use.");
      } catch (error) { orgNotice(error.message, true); }
    });
  }
}

async function orgConnectionsPage(content) {
  const [templates, connections] = await Promise.all([
    api("/api/org/connection-catalog"), api("/api/org/connections")]);
  const intro = add(content, "section", "", "org-panel");
  add(intro, "h2", "Choose an app");
  add(intro, "p", "Credentials stay in the private connection vault. Connecting an app does not approve a workflow action.");
  for (const template of templates) {
    const panel = add(content, "section", "", "org-panel org-onboarding-form");
    add(panel, "h3", template.label);
    add(panel, "p", template.verification);
    add(panel, "small", `Can request: ${template.actions.map(action => `${action.label} (${action.effect})`).join(", ") || "Actions discovered after setup"}`);
    add(panel, "p", `Permissions: ${template.scopes.join(", ") || "Review each discovered action"}`);
    add(panel, "small", `Triggers: ${template.triggers.join(", ") || "None configured"}`);
    add(panel, "p", `Health check: ${template.health_check}. Up to ${template.rate_per_minute} calls per minute.`);
    add(panel, "small", template.error_handling);
    add(panel, "small", template.revocation);
    if (template.setup === "oauth") {
      orgButton(panel, "Connect with Google", "org-button primary", async () => {
        try {
          const result = await api("/api/org/connections/gmail/start", { method: "POST" });
          window.location.assign(result.authorization_url);
        } catch (error) { orgNotice(error.message, true); }
      });
      continue;
    }
    const form = add(panel, "form", "", "org-capability-form");
    if (template.setup === "remote_mcp") {
      connectionField(form, "Public HTTPS MCP URL", "endpoint_url");
      connectionField(form, "Account or workspace label", "account_label");
    }
    for (const field of template.private_fields) {
      const input = connectionField(form,
        field === "token" ? "Private access token (if required)" : field, field, true);
      if (template.setup === "remote_mcp" && field === "token") input.required = false;
    }
    const button = add(form, "button", "Connect and verify", "org-button primary");
    button.type = "submit";
    form.onsubmit = async event => {
      event.preventDefault();
      button.disabled = true;
      const values = Object.fromEntries(template.private_fields.map(field => [field, form.elements[field].value]));
      const payload = { template: template.key, values };
      if (template.setup === "remote_mcp") {
        payload.endpoint_url = form.elements.endpoint_url.value.trim();
        payload.account_label = form.elements.account_label.value.trim();
      }
      try {
        await api("/api/org/connections", { method: "POST", body: JSON.stringify(payload) });
        form.reset();
        await window.renderOrganizationManagement(true);
        orgNotice(template.setup === "remote_mcp"
          ? "MCP server verified. The account label is supplied by you; review each action before use."
          : `${template.label} account verified.`);
      } catch (error) { orgNotice(error.message, true); button.disabled = false; }
    };
  }
  const saved = add(content, "section", "", "org-panel");
  add(saved, "h2", "Your connections");
  if (!connections.length) add(saved, "p", "No apps connected yet.");
  for (const connection of connections) {
    const row = add(saved, "div", "", "org-source-row");
    add(row, "strong", `${connection.template} · ${connection.account}`);
    add(row, "p", `Status: ${connection.health}${connection.repair_reason ? ` · ${connection.repair_reason}` : ""}`);
    add(row, "small", `Granted scopes: ${connection.scopes.join(", ") || "Not verified by this MCP server"}`);
    if (connection.health !== "healthy") add(row, "p", "Reconnect this account. If its tools changed, review the actions and workflow tests again.");
    const template = templates.find(item => item.key === connection.template);
    if (template?.setup === "oauth") {
      orgButton(row, "Reconnect with Google", "org-button secondary", async () => {
        try {
          const result = await api("/api/org/connections/gmail/start", { method: "POST",
            body: JSON.stringify({ connection_id: connection.id }) });
          window.location.assign(result.authorization_url);
        } catch (error) { orgNotice(error.message, true); }
      });
    } else if (template) {
      const reconnect = add(row, "form", "", "org-capability-form");
      if (template.setup === "remote_mcp") {
        const url = connectionField(reconnect, "Public HTTPS MCP URL", "endpoint_url");
        url.value = connection.endpoint_url;
        const account = connectionField(reconnect, "Account or workspace label", "account_label");
        account.value = connection.account;
      }
      for (const field of template.private_fields) {
        const input = connectionField(reconnect,
          field === "token" ? "New private access token (if required)" : field, field, true);
        if (template.setup === "remote_mcp" && field === "token") input.required = false;
      }
      const button = add(reconnect, "button", "Reconnect this account", "org-button secondary");
      button.type = "submit";
      reconnect.onsubmit = async event => {
        event.preventDefault();
        button.disabled = true;
        const values = Object.fromEntries(template.private_fields.map(field =>
          [field, reconnect.elements[field].value]));
        const payload = { template: template.key, values };
        if (template.setup === "remote_mcp") {
          payload.endpoint_url = reconnect.elements.endpoint_url.value.trim();
          payload.account_label = reconnect.elements.account_label.value.trim();
        }
        try {
          const updated = await api(`/api/org/connections/${connection.id}`, { method: "PUT",
            body: JSON.stringify(payload) });
          reconnect.reset();
          await window.renderOrganizationManagement(true);
          orgNotice(updated.health === "healthy" ? "Account reconnected. Pending work can be reviewed and retried."
            : "Tool schema changed. Review actions and workflow tests before retrying.");
        } catch (error) { orgNotice(error.message, true); button.disabled = false; }
      };
    }
    orgButton(row, "Check connection", "org-button secondary", async () => {
      try {
        const health = await api(`/api/org/connections/${connection.id}/health`, { method: "POST" });
        await window.renderOrganizationManagement(true);
        orgNotice(health.health === "healthy" ? "Account verified." : health.repair_reason, health.health !== "healthy");
      } catch (error) { orgNotice(error.message, true); }
    });
    orgButton(row, "Disconnect", "org-button secondary", async () => {
      try {
        await api(`/api/org/connections/${connection.id}`, { method: "DELETE" });
        await window.renderOrganizationManagement(true);
        orgNotice("Connection revoked in Austra. Revoke its token at the provider too.");
      } catch (error) { orgNotice(error.message, true); }
    });
    if (connection.template === "remote_mcp") connectionActions(row, connection);
  }
}
