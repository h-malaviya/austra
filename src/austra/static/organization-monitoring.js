window.orgMonitoringPage = async content => {
  const [health, cases] = await Promise.all([
    api("/api/org/observability"), api("/api/org/review-cases")
  ]);
  const summary = add(content, "section", "", "org-card");
  add(summary, "h2", "Operational health");
  const rows = [
    ["Resolved tasks", health.metrics.task_resolved || 0],
    ["Cost per resolved task", health.cost_per_resolved_task_usd == null
      ? "No resolved task cost yet" : `$${Number(health.cost_per_resolved_task_usd).toFixed(4)}`],
    ["Retrieval misses", health.metrics.retrieval_miss || 0],
    ["Citation failures", health.metrics.citation_failure || 0],
    ["Token budget breaches", health.metrics.token_budget_breach || 0],
    ["Connection failures", health.metrics.connection_failure || 0],
    ["Latency p50 / p95", `${health.latency_ms.p50 ?? "—"} / ${health.latency_ms.p95 ?? "—"} ms`],
    ["Mean time to repair", health.mean_repair_time_ms == null
      ? "No repairs yet" : `${Math.round(Number(health.mean_repair_time_ms) / 60000)} min`],
    ["Released build defects", health.released_build_defects],
    ["Rollback signals", health.rollback_signals],
    ["Current embeddings", `${health.embedding_freshness.current_model_chunks} / ${health.embedding_freshness.current_chunks}`],
    ["Sources awaiting extraction", health.source_freshness.pending_or_failed]
  ];
  const table = add(summary, "table", "", "org-table");
  const body = add(table, "tbody");
  rows.forEach(([label, value]) => {
    const row = add(body, "tr");
    add(row, "th", label);
    add(row, "td", String(value));
  });
  const review = add(content, "section", "", "org-card");
  add(review, "h2", "Open review cases");
  orgButton(review, "Check expiring policies", "org-button secondary", async () => {
    try {
      await api("/api/org/review-cases/scan-expiry", { method: "POST" });
      await window.renderOrganizationManagement(true);
    } catch (error) { orgNotice(error.message, true); }
  });
  if (!cases.length) add(review, "p", "No open cases.");
  cases.forEach(item => {
    const card = add(review, "article", "", "org-card");
    add(card, "h3", item.trigger.replaceAll("_", " "));
    add(card, "p", item.impact);
    add(card, "p", `Owner: ${item.owner_user_id}`);
    add(card, "p", `Next: ${item.next_step}`);
    const sources = item.citations.map(source => Object.entries(source)
      .map(([name, value]) => `${name}: ${value}`).join(", ")).join("; ");
    add(card, "small", `Sources: ${sources}`);
    orgButton(card, "Mark reviewed", "org-button secondary", async () => {
      try {
        await api(`/api/org/review-cases/${item.id}/resolve`, { method: "POST" });
        await window.renderOrganizationManagement(true);
      } catch (error) { orgNotice(error.message, true); }
    });
  });
};
