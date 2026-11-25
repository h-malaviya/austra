window.orgMonitoringPage = async content => {
  const [health, cases] = await Promise.all([
    api("/api/org/observability"), api("/api/org/review-cases")
  ]);

  const dashboard = add(content, "div", "", "org-monitoring");
  const summary = add(dashboard, "div", "", "org-stats");
  const cost = health.cost_per_resolved_task_usd;
  orgStat(summary, "Open review cases", cases.length, "Awaiting review");
  orgStat(summary, "Resolved tasks", health.metrics.task_resolved || 0, "Recorded task outcomes");
  orgStat(summary, "Cost per resolved task", cost == null ? "No data" : `$${Number(cost).toFixed(4)}`,
    cost == null ? "No resolved task cost yet" : "Average recorded cost in USD");

  orgMonitoringReviewQueue(dashboard, cases);

  const metrics = add(dashboard, "div", "", "org-monitoring-metrics");
  orgMonitoringMetricPanel(metrics, "Runtime quality", [
    ["Retrieval misses", health.metrics.retrieval_miss || 0],
    ["Citation failures", health.metrics.citation_failure || 0],
    ["Token budget breaches", health.metrics.token_budget_breach || 0],
    ["Connection failures", health.metrics.connection_failure || 0],
    ["Latency p50 / p95", `${health.latency_ms.p50 ?? "—"} / ${health.latency_ms.p95 ?? "—"} ms`]
  ]);
  orgMonitoringMetricPanel(metrics, "Release and evidence health", [
    ["Mean time to repair", health.mean_repair_time_ms == null
      ? "No repairs yet" : `${Math.round(Number(health.mean_repair_time_ms) / 60000)} min`],
    ["Released build defects", health.released_build_defects],
    ["Rollback signals", health.rollback_signals],
    ["Current embeddings", `${health.embedding_freshness.current_model_chunks} / ${health.embedding_freshness.current_chunks}`],
    ["Sources awaiting extraction", health.source_freshness.pending_or_failed]
  ]);
  if (health.automatic_email) {
    const email = health.automatic_email;
    orgMonitoringMetricPanel(metrics, "Automatic supplier replies", [
      ["Sent", email.sent || 0], ["Recorded without a send", email.completed || 0],
      ["Queued or retrying", (email.pending || 0) + (email.sending || 0)],
      ["Needs review", email.needs_review || 0], ["Delivery unconfirmed", email.uncertain || 0]
    ]);
  }
};

function orgMonitoringMetricPanel(parent, title, rows) {
  const panel = add(parent, "section", "", "org-panel");
  add(add(panel, "div", "", "org-panel-head"), "h2", title);
  const values = add(panel, "dl", "", "org-monitoring-values");
  for (const [label, value] of rows) {
    const row = add(values, "div");
    add(row, "dt", label);
    add(row, "dd", String(value));
  }
}

function orgMonitoringReviewQueue(parent, cases) {
  const queue = add(parent, "section", "", "org-panel org-monitoring-review");
  const heading = add(queue, "div", "", "org-panel-head");
  add(heading, "h2", "Review queue");
  orgMonitoringAction(heading, "Check expiring policies", "/api/org/review-cases/scan-expiry");
  if (!cases.length) add(queue, "p", "No open cases.", "org-monitoring-empty");

  cases.forEach(item => {
    const card = add(queue, "article", "", "org-monitoring-case");
    const trigger = item.trigger.replaceAll("_", " ");
    const header = add(card, "div", "", "org-monitoring-case-head");
    add(header, "h3", trigger.charAt(0).toUpperCase() + trigger.slice(1));
    orgMonitoringAction(header, "Mark reviewed", `/api/org/review-cases/${item.id}/resolve`);
    add(card, "p", item.impact);
    const details = add(card, "dl", "", "org-monitoring-case-details");
    add(details, "dt", "Next step");
    add(details, "dd", item.next_step);
    add(details, "dt", "Owner");
    add(details, "dd", item.owner_user_id);
    const evidence = add(card, "details", "", "org-monitoring-evidence");
    add(evidence, "summary", `Evidence sources (${item.citations.length})`);
    const sources = add(evidence, "ul");
    for (const source of item.citations) {
      add(sources, "li", Object.entries(source)
        .map(([name, value]) => `${name.replaceAll("_", " ")}: ${value}`).join(", "));
    }
  });
}

function orgMonitoringAction(parent, label, endpoint) {
  const button = orgButton(parent, label, "org-button secondary", async () => {
    button.disabled = true;
    try {
      await api(endpoint, { method: "POST" });
      if (button.isConnected && !button.closest("[hidden]")) {
        await window.renderOrganizationManagement(true);
      }
    } catch (error) {
      if (button.isConnected && !button.closest("[hidden]")) orgNotice(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}
