function capabilityOption(select, value, label) {
  const option = add(select, "option", label);
  option.value = value;
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

}

function purchaseWorkflowGraph(content, view, selectAction, selectedStep) {
  if (!window.AustraA2UI) return;
  const panel = add(content, "section", "", "org-panel org-capability-panel");
  add(panel, "h3", "Workflow steps");
  const steps = [
    ["submit_order", "Enter order", "Record the product and quantity in kilograms.", "Check verified stock."],
    ["reserve_stock", "Reserve stock", "Reserve no more than available stock or open demand.", "If demand remains, draft a purchase request."],
    ["draft_po", "Draft purchase", "Use the remaining quantity and a reviewed supplier price.", "Review the purchase request."],
    ["approve_po", "Approve purchase", "Requests above the approval limit go to escalation.", "Prepare an email for an approved request."],
    ["propose_email", "Draft email", "Review the recipient, subject and message.", "Approve the exact proposal."],
    ["approve_email", "Approve email", "Approval queues the message for delivery.", "Send from the approved Gmail account."],
    ["email_jobs", "Send and track replies", "Send the approved message once. Follow replies in the original Gmail thread.", ""]
  ].filter(([key]) => key === "email_jobs" ? view.commands.includes("approve_email") : view.commands.includes(key))
    .map(([key, label, detail, next]) => ({key, label, detail, next}));
  if (!steps.length) { panel.remove(); return; }
  const id = "purchase_steps";
  window.AustraA2UI.mount(panel, {id, messages: [
    {version: "v0.9.1", createSurface: {surfaceId: id, catalogId: "https://austra.local/catalogs/report.json"}},
    {version: "v0.9.1", updateComponents: {surfaceId: id,
      components: [{id: "root", component: "AustraWorkflowGraph", steps, selectedStep}]}}
  ]}, () => {});
  panel.addEventListener("austra-workflow-step", event => selectAction?.(event.detail));
}

function renderCapabilityEmailJobs(content, view, departmentId, { report, emailReference, connectGmail }) {
  const panel = add(content, "section", "", "org-panel org-capability-panel");
  panel.dataset.workflowEmail = "";
  add(panel, "h3", view.capabilities ? "Workflow email" : "Purchase request email");
  const jobs = add(add(panel, "label", "Email reference"), "select");
  const detail = add(panel, "div");
  const replyRefreshMs = 5000;
  let replyRefresh;
  view.email_jobs.forEach(job => capabilityOption(jobs, job.id, job.reference));
  if (!view.email_jobs.length) {
    panel.replaceChildren();
    add(panel, "p", "Approve an exact email draft before choosing Gmail and sending.");
    return;
  }
  const preferred = view.email_jobs.find(job => job.reference === emailReference);
  if (preferred) jobs.value = preferred.id;
  const request = (path, payload) => api(path, {method: "POST", body: JSON.stringify(payload || {})});
  function button(parent, label, work) {
    const control = orgButton(parent, label, "org-button secondary", async () => {
      control.disabled = true;
      try { await work(); }
      catch (error) { report(error.message, true); }
      finally { control.disabled = false; }
    });
    return control;
  }
  function show() {
    clearTimeout(replyRefresh);
    const job = view.email_jobs.find(item => item.id === jobs.value);
    if (!job) return;
    detail.replaceChildren();
    add(detail, "p", job.subject);
    add(detail, "p", "To " + job.recipient);
    const base = `/api/org/email-jobs/${departmentId}/${job.id}`;
    if (job.state === "delivered") {
      const caseStatus = add(detail, "p");
      caseStatus.setAttribute("role", "status");
      caseStatus.hidden = true;
      const timeline = add(detail, "div");
      const tracking = add(detail, "p");
      const automaticTracking = add(detail, "p", "Approved replies will be sent automatically in this thread.");
      const replyCount = add(timeline, "strong");
      replyCount.setAttribute("aria-live", "polite");
      const items = add(timeline, "ol");
      let evidence = {};
      let renderedEvidence = "";
      const trackingMessages = {
        tracking: "Checking this Gmail thread automatically",
        retrying: "Austra will retry pending Gmail checks.",
        ended: "Automatic checks have ended. You can still check this thread.",
        manual: "Check now to start tracking this thread."
      };
      const recoveryMessages = {
        retrying: "Reply saved. Austra will retry processing its content automatically.",
        applying: "Reply content saved. Austra will resume the approved record update.",
        recovered: "Austra recovered this reply’s content and resumed processing.",
        needs_review: "Reply saved. Its content needs review after automatic attempts."
      };
      const automaticMessages = {
        pending: "Automatic reply queued.", sending: "Sending an approved reply in this thread.",
        sent: "Austra replied in this Gmail thread.", completed: "Austra processed this reply.",
        needs_review: "Austra saved this reply for review.", uncertain: "Reply delivery needs verification.",
      };
      let checking = false;
      let requestVersion = 0;
      function applyReplies(result) {
        if (result.tracked_record) {
          const {reference, state, revision} = result.tracked_record;
          const label = `${reference} · ${workflowLabel(state)} · Revision ${revision}`;
          if (caseStatus.textContent !== label) caseStatus.textContent = label;
          caseStatus.hidden = false;
        }
        evidence = result.evidence || evidence;
        job.record_updates = result.record_updates || job.record_updates || {};
        job.recovery = result.recovery || job.recovery || {};
        job.automatic_actions = result.automatic_actions || job.automatic_actions || {};
        job.automatic_enabled = result.automatic_enabled ?? job.automatic_enabled ?? false;
        automaticTracking.hidden = !job.automatic_enabled;
        job.replies = result.replies || (result.reply ? [result.reply] : []);
        job.tracking = result.tracking;
        job.tracking_until = result.tracking_until;
        const text = job.replies.length
          ? `${job.replies.length} ${job.replies.length === 1 ? "reply" : "replies"} received`
          : "Sent · Awaiting replies";
        if (replyCount.textContent !== text) replyCount.textContent = text;
        tracking.textContent = trackingMessages[job.tracking] || trackingMessages.tracking;
        const signature = JSON.stringify([job.replies, evidence, job.record_updates, job.recovery, job.automatic_actions]);
        if (signature === renderedEvidence) return;
        renderedEvidence = signature;
        const open = new Set([...items.querySelectorAll("details[open]")].map(item => item.dataset.replyId));
        items.replaceChildren();
        job.replies.forEach(reply => {
          const row = add(items, "li");
          add(row, "strong", reply.subject || "(No subject)");
          add(row, "p", `${reply.from} · ${new Date(reply.received_ms).toLocaleString()}`);
          const update = job.record_updates[reply.message_id];
          const recovery = job.recovery[reply.message_id]?.state;
          if (recoveryMessages[recovery]) add(row, "p", recoveryMessages[recovery]);
          const automatic = job.automatic_actions[reply.message_id];
          if (automaticMessages[automatic?.state]) add(row, "p", automaticMessages[automatic.state]);
          if (automatic?.findings?.length) {
            const findings = add(row, "ul");
            for (const finding of automatic.findings) add(findings, "li", finding);
          }
          if (update?.reason) add(row, "p", update.reason);
          if (update?.updated?.length) add(row, "p", `Record updated: ${update.updated.map(workflowLabel).join(", ")}`);
          if (update?.review?.length) add(row, "p", `Review missing or conflicting values for ${update.review.map(workflowLabel).join(", ")}.`);
          if (update?.waiting?.length && !recovery) add(row, "p", `Waiting for reply content for ${update.waiting.map(workflowLabel).join(", ")}.`);
          const replyContent = evidence[reply.message_id];
          if (replyContent) {
            const details = add(row, "details");
            details.dataset.replyId = reply.message_id;
            details.open = open.has(reply.message_id);
            add(details, "summary", "Reply and documents");
            if (replyContent.body) add(details, "pre", replyContent.body);
            for (const attachment of replyContent.attachments || []) {
              add(details, "strong", attachment.name);
              if (attachment.status !== "extracted") add(details, "p", attachment.reason || "Needs review");
              for (const excerpt of attachment.excerpts || []) add(details, "pre", excerpt.text);
            }
          }
        });
        items.hidden = !job.replies.length;
      }
      const threadLink = add(detail, "a", "Open the original thread in Gmail");
      threadLink.hidden = true;
      threadLink.target = "_blank";
      threadLink.rel = "noopener noreferrer";
      function showThread() {
        const thread = job.replies.at(-1)?.thread_id;
        if (!thread) return;
        threadLink.href = `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(thread)}`;
        threadLink.hidden = false;
      }
      function scheduleRefresh() {
        replyRefresh = setTimeout(async () => {
          if (!timeline.isConnected) return;
          const version = requestVersion;
          try {
            if (!document.hidden && !checking) {
              const result = await api(base + "/replies");
              if (timeline.isConnected && version === requestVersion) {
                applyReplies(result);
                showThread();
              }
            }
          } catch {
            if (timeline.isConnected) tracking.textContent = "Couldn’t refresh replies. Austra will retry.";
          } finally {
            if (timeline.isConnected) scheduleRefresh();
          }
        }, replyRefreshMs);
      }
      applyReplies(job);
      showThread();
      button(detail, "Check for Gmail replies", async () => {
        requestVersion += 1;
        checking = true;
        try {
          const previous = job.replies.length;
          const result = await request(base + "/check-reply");
          if (!timeline.isConnected) return;
          applyReplies(result);
          const added = job.replies.length - previous;
          report(added ? `${added} new ${added === 1 ? "reply" : "replies"} saved.` : "No new matching replies.");
        } finally {
          checking = false;
        }
      });
      scheduleRefresh();
    } else {
      add(detail, "p", job.state === "repair"
        ? "Delivery needs review. Check whether Gmail sent this email before retrying."
        : "Queued for delivery");
      const approval = add(detail, "div");
      button(approval, "Choose Gmail account", async () => {
        const connections = (await api("/api/org/connections"))
          .filter(connection => connection.template === "gmail" && connection.health === "healthy");
        if (!connections.length) { connectGmail(approval); return; }
        approval.replaceChildren();
        const select = add(add(approval, "label", "Gmail account"), "select");
        connections.forEach(account => capabilityOption(select, account.id, account.account));
        const access = add(approval, "p");
        const authorize = button(approval, "Authorize Gmail reply content", async () => connectGmail(approval, select.value));
        const showAccess = () => {
          const account = connections.find(item => item.id === select.value);
          const content = account?.scopes?.includes("https://www.googleapis.com/auth/gmail.readonly");
          access.textContent = content
            ? "Austra can process reply text and supported attachments in the background."
            : "This Gmail grant can track replies. Authorize reply content to process messages and attachments.";
          authorize.hidden = Boolean(content);
        };
        select.onchange = showAccess;
        showAccess();
        button(approval, "Approve Gmail for this workflow", async () => {
          await request(base + "/approve-connection", {connection_id: select.value});
          approval.replaceChildren();
          report("Gmail approved. Preview the exact email before sending.");
        });
      });
      const preview = add(detail, "div");
      button(detail, "Preview approved email", async () => {
        const result = await request(base + "/preview");
        if (jobs.value !== job.id) return;
        preview.replaceChildren();
        add(preview, "p", "From " + result.preview.account);
        add(preview, "p", "To " + result.preview.arguments.to);
        add(preview, "strong", result.preview.arguments.subject);
        add(preview, "pre", result.preview.arguments.body);
        const attachment = result.preview.workflow_email?.attachment;
        if (attachment) {
          const link = add(preview, "a", `Preview attachment: ${attachment.name}`);
          link.href = `data:application/pdf;base64,${attachment.base64}`;
          link.download = attachment.name;
        }
        const reminder = add(add(preview, "label", "Follow-up reminder"), "select");
        for (const [value, label] of [["", "None"], ["1", "After 1 day"], ["2", "After 2 days"], ["7", "After 7 days"]]) {
          capabilityOption(reminder, value, label);
        }
        const emailLabel = add(preview, "label", "Also email the workflow owner about replies and reminders");
        const emailAlerts = add(emailLabel, "input");
        emailAlerts.type = "checkbox";
        let automaticReplies;
        const config = result.preview.workflow_email?.automatic_email;
        if (config) {
          const reviewed = add(preview, "details");
          add(reviewed, "summary", "Review automatic reply rules for this recipient");
          renderAutomaticReplyRules(reviewed, config);
          const automaticLabel = add(preview, "label", "Authorize these automatic replies in this Gmail thread ");
          automaticReplies = add(automaticLabel, "input");
          automaticReplies.type = "checkbox";
        }
        button(preview, "Send this exact email", async () => {
          const sent = await request(`/api/org/email-jobs/${job.id}/deliver`, {
            department_id: departmentId, proposal_id: result.proposal_id,
            follow_up_days: reminder.value ? Number(reminder.value) : null,
            owner_email_alerts: emailAlerts.checked,
            ...(automaticReplies ? {automatic_replies: automaticReplies.checked} : {}),
          });
          job.state = sent.state;
          if (sent.state === "delivered") {
            job.tracking = "tracking";
            job.automatic_enabled = Boolean(automaticReplies?.checked);
          }
          show();
          report(sent.reason || "Delivery confirmed by Gmail.", sent.state !== "delivered");
        });
      });
    }
  }
  jobs.onchange = show;
  show();
}

const workflowLabels = {
  submit_order: "Enter order", reserve_stock: "Reserve stock", draft_po: "Draft purchase",
  approve_po: "Approve purchase", propose_email: "Draft email", approve_email: "Approve email",
  email_jobs: "Send and track replies"
};

function workflowLabel(key) {
  return workflowLabels[key] || key.replace(/^command\./, "").replaceAll("_", " ").replaceAll(".", " · ");
}

function workflowRules(panel, view) {
  if (!view.rules?.length) return;
  add(panel, "h3", "Approved rules");
  const rules = add(panel, "ul");
  view.rules.forEach(rule => {
    const label = workflowLabel(rule.decision_key.replace(/^command\./, ""));
    const permission = rule.required_permission === "workflows.approve"
      ? "requires workflow approval" : "requires workflow run access";
    const limit = rule.limit == null ? "" : ` · limit ${rule.unit} ${rule.limit}. Higher amounts escalate.`;
    add(rules, "li", `${label} · ${permission}${limit}`);
  });
}

async function orgCapabilityPage(content) {
  const items = await api("/api/org/released-workflows");
  const title = add(content, "div", "", "org-page-heading");
  add(title, "h2", "Released workflows");
  if (!Array.isArray(items) || !items.length) {
    add(content, "p", "No workflows have been released yet.");
    return;
  }
  const list = add(content, "div", "", "org-panel org-capability-panel");
  const detail = add(content, "div");
  let version = 0;
  async function show(item) {
    const current = ++version;
    detail.replaceChildren();
    try {
      const view = await api(`/api/org/capability/${item.department_id}?build_id=${item.build_id}`);
      if (current !== version || !detail.isConnected) return;
      const card = add(detail, "section", "", "org-panel org-capability-panel");
      add(card, "h3", item.name);
      add(card, "p", item.department);
      if (view.capabilities) {
        for (const capability of view.capabilities) {
          const steps = add(card, "ol");
          for (const step of capability.transitions) {
            const detail = step.from ? `From ${workflowLabel(step.from)} to ${workflowLabel(step.to)}`
              : `Starts a record in ${workflowLabel(step.to)}`;
            add(steps, "li", `${workflowLabel(step.key)} · ${detail}`);
          }
          if (capability.email_document) {
            add(steps, "li", `Generate and preview “${capability.email_document.title}” before sending.`);
          }
          if (capability.reply_mapping) {
            const mapping = capability.reply_mapping;
            add(steps, "li", `Update ${mapping.fields.map(field => workflowLabel(field.target)).join(", ")} from replies while the record is ${mapping.states.map(workflowLabel).join(" or ")}.`);
          }
        }
      } else purchaseWorkflowGraph(card, view);
      workflowRules(card, view);
    } catch (error) {
      if (current === version) add(detail, "p", error.message, "org-error");
    }
  }
  for (const item of items) {
    orgButton(list, `${item.name} · ${item.department}`, "org-button secondary", () => show(item));
  }
  const query = new URLSearchParams(window.location.search);
  const initial = items.find(item => item.department_id === query.get("department")
    && (!query.get("build") || item.build_id === query.get("build")));
  if (initial) await show(initial);
}
