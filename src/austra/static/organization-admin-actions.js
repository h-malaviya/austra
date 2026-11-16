function orgFormModal(title, description, fields, submitLabel, onSubmit) {
  const previousFocus = document.activeElement;
  const backdrop = add(document.body, "div", "", "platform-backdrop");
  const dialog = add(backdrop, "section", "", "org-modal");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  const heading = add(dialog, "h2", title);
  heading.id = "org-modal-title";
  dialog.setAttribute("aria-labelledby", heading.id);
  add(dialog, "p", description, "org-modal-description");
  const form = add(dialog, "form");
  const grid = add(form, "div", "", "org-form-grid");
  fields.forEach(field => {
    const wrapper = field.type === "checkboxes"
      ? add(grid, "fieldset", "", "org-field org-permission-field")
      : add(grid, "label", "", "org-field");
    add(wrapper, field.type === "checkboxes" ? "legend" : "span", field.label);
    if (field.type === "checkboxes") {
      const choices = add(wrapper, "div", "", "org-permission-grid");
      field.options.forEach(([value, label]) => {
        const choice = add(choices, "label", "", "org-check");
        const input = add(choice, "input");
        input.type = "checkbox";
        input.name = field.key;
        input.value = value;
        input.checked = (field.value || []).includes(value);
        add(choice, "span", label);
      });
      return;
    }
    const input = add(wrapper, field.type === "select" ? "select" : "input");
    input.name = field.key;
    input.required = Boolean(field.required);
    if (field.type === "select") {
      field.options.forEach(([value, label]) => {
        const option = add(input, "option", label);
        option.value = value;
      });
      input.value = field.value || field.options[0]?.[0] || "";
    } else {
      input.type = field.type || "text";
      input.value = field.value || "";
      input.autocomplete = "off";
      if (field.key === "slug") input.pattern = "[a-z0-9]+(?:-[a-z0-9]+)*";
      if (field.key === "username") input.pattern = "[A-Za-z0-9._-]{3,64}";
      if (field.maxLength) input.maxLength = field.maxLength;
      if (field.minLength) input.minLength = field.minLength;
    }
  });
  const feedback = add(form, "p", "", "org-form-feedback");
  feedback.setAttribute("role", "alert");
  const actions = add(form, "div", "", "org-modal-actions");
  orgButton(actions, "Cancel", "org-button secondary", close);
  const submit = add(actions, "button", submitLabel, "org-button primary");
  submit.type = "submit";
  function close() {
    backdrop.remove();
    if (!document.querySelector(".platform-backdrop")) previousFocus?.focus?.();
  }
  backdrop.onclick = event => { if (event.target === backdrop) close(); };
  dialog.onkeydown = event => {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll("input, select, button")];
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    submit.disabled = true;
    feedback.textContent = "";
    const formData = new FormData(form);
    const values = Object.fromEntries(formData.entries());
    fields.filter(field => field.type === "checkboxes")
      .forEach(field => { values[field.key] = formData.getAll(field.key); });
    try {
      fields.filter(field => field.required && field.type !== "checkboxes").forEach(field => {
        if (!String(values[field.key] || "").trim()) throw new Error(`${field.label} is required`);
      });
      await onSubmit(values);
      close();
    }
    catch (error) { feedback.textContent = error.message; }
    finally { submit.disabled = false; }
  };
  form.querySelector("input, select")?.focus();
}

function orgCreateDepartment() {
  orgFormModal("Create department", "Add a space for items, people, and roles.", [
    { label: "Department name", key: "name", required: true, maxLength: 120 },
    { label: "Stable slug", key: "slug", required: true, maxLength: 64 }
  ], "Create department", async values => {
    const result = await api("/api/org/departments", { method: "POST", body: JSON.stringify(values) });
    window.history.pushState({}, "", `${window.orgDepartmentsPath}/${result.id}`);
    await window.renderOrganizationManagement(true);
  });
}

function orgRenameDepartment(department) {
  orgFormModal("Rename department", "The stable slug remains the same.", [
    { label: "Department name", key: "name", value: department.name, required: true, maxLength: 120 }
  ], "Save name", async values => {
    await api(`/api/org/departments/${department.id}`, {
      method: "PATCH", body: JSON.stringify(values)
    });
    await window.renderOrganizationManagement(true);
    orgNotice("Department name saved.");
  });
}

function orgArchiveDepartment(department) {
  orgFormModal("Archive department", "Access assignments will be revoked. Saved items must be moved or archived first.", [
    { label: "Reason for archiving", key: "reason", required: true, minLength: 5, maxLength: 500 }
  ], "Archive department", async values => {
    const impact = await api(`/api/org/departments/${department.id}/archive-impact`);
    const count = Object.values(impact.objects).reduce((sum, value) => sum + value, 0);
    if (count) throw new Error(`${count} saved item${count === 1 ? "" : "s"} must be moved or archived first.`);
    await api(`/api/org/departments/${department.id}`, {
      method: "DELETE", body: JSON.stringify(values)
    });
    await window.renderOrganizationManagement(true);
    orgNotice("Department archived.");
  });
}

function orgCreateEmployee() {
  orgFormModal("Create employee", "A temporary password will be shown once after creation.", [
    { label: "Username", key: "username", required: true, maxLength: 64 },
    { label: "Full name", key: "full_name", required: true, maxLength: 120 },
    { label: "Email", key: "email", type: "email", required: true, maxLength: 254 },
    { label: "Contact number", key: "contact_number", type: "tel", required: true, maxLength: 40 }
  ], "Create employee", async values => {
    const result = await api("/api/org/users", { method: "POST", body: JSON.stringify(values) });
    await window.renderOrganizationManagement(true);
    platformCredential(result, "Employee created");
  });
}

function orgEditEmployee(user) {
  orgFormModal("Edit employee", "Update profile details and account status.", [
    { label: "Username", key: "username", value: user.username, required: true, maxLength: 64 },
    { label: "Full name", key: "full_name", value: user.full_name, required: true, maxLength: 120 },
    { label: "Email", key: "email", type: "email", value: user.email, required: true, maxLength: 254 },
    { label: "Contact number", key: "contact_number", type: "tel", value: user.contact_number, required: true, maxLength: 40 },
    { label: "Status", key: "status", type: "select", required: true, value: user.status,
      options: [["active", "Active"], ["suspended", "Suspended"]] }
  ], "Save employee", async values => {
    await api(`/api/org/users/${user.id}`, { method: "PATCH", body: JSON.stringify(values) });
    await window.renderOrganizationManagement(true);
    orgNotice("Employee details saved.");
  });
}

async function orgResetPassword(user) {
  if (!await showModal({ title: "Reset employee password?",
    message: `A new temporary password will be issued for ${user.username}. Their current sessions will end.`,
    confirmLabel: "Reset password", cancelLabel: "Cancel" })) return;
  try {
    const result = await api(`/api/org/users/${user.id}/reset-password`, { method: "POST" });
    await window.renderOrganizationManagement(true);
    platformCredential({ ...result, username: user.username }, "Password reset");
  } catch (error) { orgNotice(error.message, true); }
}

function orgAssignRole(department, users, roles) {
  orgFormModal("Assign role", `Grant access within ${department.name}.`, [
    { label: "Employee", key: "user_id", type: "select", required: true,
      options: users.map(user => [user.id, user.username]) },
    { label: "Role", key: "role_id", type: "select", required: true,
      options: roles.map(role => [role.id, role.name]) }
  ], "Assign role", async values => {
    await api(`/api/org/departments/${department.id}/members/${values.user_id}/roles`, {
      method: "POST", body: JSON.stringify({ role_id: values.role_id })
    });
    await window.renderOrganizationManagement(true);
    orgNotice("Role assigned.");
  });
}

function orgRoleForm(department, role = null) {
  const fields = [
    { label: "Role name", key: "name", value: role?.name, required: true, maxLength: 120 }
  ];
  if (!role) fields.push({ label: "Stable slug", key: "slug", required: true, maxLength: 64 });
  fields.push(
    { label: "Permissions", key: "permissions", type: "checkboxes",
      value: role?.permissions || [], options: window.orgPermissions },
    { label: "Reason for change", key: "reason", required: true, minLength: 3, maxLength: 500 }
  );
  orgFormModal(role ? "Edit role" : "Create role",
    "Choose the exact capabilities this role grants in this department.", fields,
    role ? "Save role" : "Create role", async values => {
      await api(`/api/org/departments/${department.id}/roles${role ? "/" + role.id : ""}`, {
        method: role ? "PATCH" : "POST", body: JSON.stringify(values)
      });
      window.orgAdminState.detailTab = "roles";
      await window.renderOrganizationManagement(true);
      orgNotice(role ? "Role updated." : "Role created.");
    });
}
