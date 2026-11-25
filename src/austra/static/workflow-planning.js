function renderAutomaticReplyRules(parent, config) {
  for (const rule of config.rules) {
    const step = add(parent, "section", "", "org-source-row");
    add(step, "strong", `When the case is ${workflowLabel(rule.state)}`);
    const trigger = [rule.attachment_name_contains && `Attachment matching “${rule.attachment_name_contains}”`,
      rule.body_contains && `New message text containing “${rule.body_contains}”`].filter(Boolean);
    if (trigger.length) add(step, "p", trigger.join(" and "));
    for (const [role, markers] of Object.entries(rule.documents || {})) {
      add(step, "p", `${workflowLabel(role)} identified by ${markers.join(", ")}`);
    }
    if (rule.reply_openers?.length) add(step, "p", `Accepted confirmations: ${rule.reply_openers.join(", ")}`);
    const checks = add(step, "ul");
    for (const check of rule.checks) {
      const source = check.name || "New message text";
      const comparison = check.source === "record" || check.labels?.length ? "must equal" : "must contain";
      const failure = check.source === "record" || check.blocking ? "Hold for review" : "Report";
      const field = check.labels?.length ? `, field ${check.labels.join(" or ")},` : "";
      const expected = check.expected_field ? `approved record field ${workflowLabel(check.expected_field)}` : `“${check.contains}”`;
      add(checks, "li", `${source}${field} ${comparison} ${expected}. ${failure} if it differs. ${check.finding}`);
    }
    for (const [label, branch] of [["When checks pass", rule.on_match], ["When checks find a discrepancy", rule.on_mismatch]]) {
      add(step, "p", label);
      if (branch.transition) add(step, "p", `Next step · ${workflowLabel(branch.transition)}`);
      for (const [key, value] of Object.entries(branch.fields || {})) {
        add(step, "p", `${workflowLabel(key)} → ${value}`);
      }
      if (branch.body) add(step, "pre", branch.body);
      if (branch.document) add(step, "p", `Attach ${branch.document.title} (PDF)`);
      if (!branch.transition && !branch.body) add(step, "p", "Keep the saved result on the timeline.");
    }
  }
}

async function orgWorkflowPlanningPage(content) {
  const path = "/api/org/workflow-plans";
  const plans = await api(path);
  if (!content.isConnected) return;
  const selected = new URLSearchParams(window.location.search).get("plan");
  const route = id => "/organization/workflow-planning?plan=" + encodeURIComponent(id);
  const post = (url, values) => api(url, {method: "POST", body: JSON.stringify(values)});
  if (!selected) {
    if (plans.departments.length) {
      const panel = add(content, "section", "", "org-panel org-onboarding-form");
      const choose = add(add(panel, "label", "Workflow department"), "select");
      for (const department of plans.departments) capabilityOption(choose, department.id, department.name);
      orgButton(panel, "Start planning", "org-button primary", async () => {
        try { orgOpenPath(route((await post(path, {department_id: choose.value})).id)); }
        catch (error) { orgNotice(error.message, true); }
      });
    }
    for (const plan of plans.plans) {
      const row = add(content, "div", "", "org-source-row");
      orgLink(row, plan.name, route(plan.id));
      add(row, "p", [plan.department, plan.owner, plan.submitted_at ? "Submitted for review" : "In progress"].filter(Boolean).join(" · "));
    }
    if (!plans.departments.length && !plans.plans.length) add(content, "p", "Ask your admin for workflow drafting access in your department.");
    return;
  }
  const summary = plans.plans.find(item => item.id === selected);
  const base = path + "/" + encodeURIComponent(selected);
  let plan = await api(base);
  if (!content.isConnected) return;
  const panel = add(content, "section", "", "org-panel org-onboarding-form");
  orgLink(panel, "All workflow plans", "/organization/workflow-planning");
  const view = add(panel, "div");
  function renderPlan() {
    view.replaceChildren();
    const messageList = add(view, "div");
    messageList.setAttribute("role", "log");
    for (const item of plan.messages) {
      add(messageList, "strong", item.role === "assistant" ? "Austra" : summary?.owner || "You");
      add(messageList, "p", item.body, "org-plan-message");
    }
    if (plan.proposal) {
      add(view, "h2", plan.proposal.name);
      const steps = add(view, "ol");
      for (const step of plan.proposal.behavior.manual_steps) add(steps, "li", step);
      if (plan.proposal.open_decisions.length) {
        add(view, "h3", "Decisions for administrator review");
        for (const question of plan.proposal.open_decisions) add(view, "p", question);
      }
      const config = plan.proposal.suggested_definition?.automatic_email;
      if (config) {
        const rules = add(view, "details");
        add(rules, "summary", "Proposed automatic messages and checks");
        renderAutomaticReplyRules(rules, config);
      }
    }
    const controls = add(view, "div", "", "org-form-actions");
    const work = (label, action) => {
      const button = orgButton(controls, label, "org-button secondary", async () => {
        for (const item of controls.querySelectorAll("button")) item.disabled = true;
        orgNotice("Austra is working…");
        try { await action(); if (view.isConnected) orgNotice(""); }
        catch (error) { if (view.isConnected) orgNotice(error.message, true); }
        finally { for (const item of controls.querySelectorAll("button")) item.disabled = false; }
      });
      return button;
    };
    if (plan.submitted_at) {
      add(view, "p", "Submitted for administrator review.");
      if (currentConfig.role === "org_admin") {
        if (plan.case_id) {
          const review = "/organization/workflow-reviews?workflow=" + plan.case_id;
          orgLink(view, "Open workflow review", review);
          const definition = plan.proposal.suggested_definition;
          const details = add(view, "details");
          add(details, "summary", "Proposed record fields and steps");
          for (const field of definition.fields) add(details, "p", `${workflowLabel(field.key)} · ${field.type}`);
          for (const step of definition.transitions) {
            add(details, "p", `${workflowLabel(step.key)} → ${workflowLabel(step.to)} · ${step.permission}`);
          }
          if (definition.email_document) add(details, "p", `${definition.email_document.title} · ${definition.email_document.fields.map(workflowLabel).join(", ")}`);
          add(details, "p", definition.failure_path);
          const decisions = plan.proposal.open_decisions.map(question =>
            [question, add(add(view, "label", question), "textarea", "org-onboarding-textarea")]);
          work("Prepare this reviewed definition", async () => {
            await post(base + "/prepare-review", {revision: plan.revision,
              resolutions: Object.fromEntries(decisions.map(([question, answer]) => [question, answer.value]))});
            orgOpenPath(review);
          });
        }
        else work("Open as workflow candidate", async () => {
          await post(base + "/import", {});
          plan = await api(base);
          renderPlan();
        });
      }
      return;
    }
    if (!plan.messages.length) add(view, "p", "Describe the workflow, who should approve it, and how Austra should handle missing or conflicting evidence.");
    const input = add(add(view, "label", "Message Austra"), "textarea", "org-onboarding-textarea");
    input.maxLength = 8000;
    const save = async (action, body = {}) => {
      plan = await post(base + "/" + action, {revision: plan.revision, ...body});
      if (view.isConnected) renderPlan();
    };
    work("Send message", () => save("messages", {body: input.value}));
    if (plan.messages.length >= 4) work("Prepare proposal", () => save("prepare"));
    if (plan.proposal?.suggested_definition) {
      work("Confirm and submit for admin review", () => save("submit"));
    }
  }
  renderPlan();
}
