function startWorkflowAlerts(isActive) {
  const button = document.getElementById("workflowAlertsButton");
  button.hidden = false;
  let known = new Set();
  let initialized = false;
  let alerts = [];
  let timer;
  const dialog = add(document.body, "dialog", "", "org-panel workflow-alert-dialog");
  dialog.setAttribute("aria-label", "Workflow notifications");
  add(dialog, "h2", "Workflow notifications");
  const actions = add(dialog, "div", "", "workflow-alert-actions");
  const browser = orgButton(actions, "Enable browser notifications", "org-button secondary", async () => {
    try {
      const permission = await Notification.requestPermission();
      browser.textContent = permission === "granted" ? "Browser notifications enabled"
        : "Browser notifications are unavailable";
      browser.disabled = permission !== "default";
    } catch {
      browser.textContent = "Browser notifications are unavailable";
      browser.disabled = true;
    }
  });
  browser.hidden = !window.Notification;
  if (window.Notification?.permission === "granted") {
    browser.textContent = "Browser notifications enabled";
    browser.disabled = true;
  }
  orgButton(actions, "Close", "org-button secondary", () => dialog.close());
  const list = add(dialog, "div");
  function render() {
    const unread = alerts.filter(item => !item.read).length;
    button.textContent = unread ? `Notifications (${unread})` : "Notifications";
    if (!dialog.open) return;
    list.replaceChildren();
    if (!alerts.length) add(list, "p", "No workflow notifications yet.");
    for (const alert of alerts) {
      const item = add(list, "article");
      add(item, "h3", alert.title);
      add(item, "p", alert.message);
      add(item, "small", new Date(alert.created_at).toLocaleString());
      if (alert.email === "attention") add(item, "p", "The email alert needs attention.");
      if (!alert.read) orgButton(item, "Mark read", "org-button secondary", async () => {
        try {
          await api(`/api/org/workflow-notifications/${alert.id}/read`, {method: "POST"});
          alert.read = true;
          render();
        } catch (error) { add(item, "p", error.message, "org-error"); }
      });
    }
  }
  button.onclick = () => { dialog.showModal(); render(); };
  async function refresh() {
    if (!isActive()) return;
    try {
      const result = await api("/api/org/workflow-notifications");
      if (!isActive() || !Array.isArray(result)) return;
      if (initialized && window.Notification?.permission === "granted") {
        for (const alert of result.filter(item => !item.read && !known.has(item.id))) {
          const popup = new Notification(alert.title, {body: alert.message, tag: alert.id});
          popup.onclick = () => { window.focus(); if (!dialog.open) button.click(); popup.close(); };
        }
      }
      known = new Set(result.map(item => item.id));
      initialized = true;
      if (JSON.stringify(alerts) === JSON.stringify(result)) return;
      alerts = result;
      render();
    } catch {
      if (isActive() && dialog.open && !list.childElementCount) {
        add(list, "p", "Couldn’t load notifications. Austra will retry.", "org-error");
      }
    } finally {
      if (isActive()) timer = setTimeout(refresh, 15000);
    }
  }
  refresh();
  return () => {
    clearTimeout(timer);
    dialog.remove();
    button.hidden = true;
    button.onclick = null;
  };
}
