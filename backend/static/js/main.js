/* AssetVault — vanilla JS: modals, DataTables, Chart.js */

/* ---------------------------- Modal helpers ---------------------------- */
function openModal(id) {
  const el = document.getElementById(id);
  el.classList.remove("hidden");
  el.classList.add("is-open");
  document.body.style.overflow = "hidden";
}

function closeModal(id) {
  const el = document.getElementById(id);
  el.classList.add("hidden");
  el.classList.remove("is-open");
  document.body.style.overflow = "";
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".modal.is-open").forEach((m) => closeModal(m.id));
});

document.addEventListener("click", (e) => {
  if (e.target.classList && e.target.classList.contains("modal")) closeModal(e.target.id);
});

/* ------------------------ Asset add / edit modal ----------------------- */
function openAssetModal(asset) {
  const form = document.getElementById("assetForm");
  const title = document.getElementById("assetModalTitle");

  if (asset) {
    title.textContent = "Edit asset";
    form.action = `/api/assets/${asset.id}/update`;
    form.serial_number.value = asset.serial_number;
    form.device_name.value = asset.device_name;
    form.category.value = asset.category;
    form.status.value = asset.status;
    form.purchase_date.value = asset.purchase_date || "";
    form.warranty_provider.value = asset.warranty_provider || "";
    form.warranty_expiry.value = asset.warranty_expiry || "";
    form.notes.value = asset.notes || "";
  } else {
    title.textContent = "Add asset";
    form.action = window.__ASSET_URLS__.create;
    form.reset();
  }
  openModal("assetModal");
}

/* --------------------------- Assign modal ----------------------------- */
function openAssignModal(asset) {
  const form = document.getElementById("assignForm");
  form.action = `/api/assets/${asset.id}/assign`;
  form.employee_id.value = "";
  form.expected_return_date.value = "";
  form.condition_at_assignment.value = "";
  form.remarks.value = "";
  document.getElementById("assignAssetLabel").textContent = `${asset.device_name} · ${asset.serial_number}`;
  openModal("assignModal");
}

/* ------------------------- Employee modal ----------------------------- */
function openEmployeeModal(employee) {
  const form = document.getElementById("employeeForm");
  const title = document.getElementById("employeeModalTitle");

  if (employee) {
    title.textContent = "Edit employee";
    form.action = `/api/employees/${employee.id}/update`;
    form.name.value = employee.name;
    form.email.value = employee.email;
    form.department.value = employee.department;
    form.job_title.value = employee.job_title || "";
  } else {
    title.textContent = "Add employee";
    form.action = "/api/employees/create";
    form.reset();
    form.department.value = "General";
  }
  openModal("employeeModal");
}

/* ------------------- Employee set-password modal (Phase 2) ------------ */
function openSetPasswordModal(employeeId, employeeName) {
  const form = document.getElementById("setPasswordForm");
  form.action = `/api/employees/${employeeId}/set-password`;
  form.reset();
  document.getElementById("setPasswordEmployeeLabel").textContent = employeeName;
  openModal("setPasswordModal");
}

/* --------------------------- DataTables ------------------------------- */
function initAssetsTable() {
  const table = $("#assetsTable").DataTable({
    pageLength: 10,
    lengthMenu: [10, 25, 50, 100],
    order: [],
    columnDefs: [{ targets: 7, orderable: false, searchable: false }],
    language: { search: "Search inventory:", emptyTable: "No assets yet — add your first device." },
  });

  $("#statusFilter").on("change", function () {
    table.column(3).search(this.value ? `^${this.value}$` : "", true, false).draw();
  });
  $("#categoryFilter").on("change", function () {
    table.column(2).search(this.value ? `^${this.value}$` : "", true, false).draw();
  });
}

function initEmployeesTable() {
  $("#employeesTable").DataTable({
    pageLength: 10,
    order: [],
    columnDefs: [{ targets: 6, orderable: false, searchable: false }],
    language: { search: "Search employees:", emptyTable: "No employees yet." },
  });
}

/* ----------------------- Admin tickets table (Phase 3) ----------------- */
function initTicketsTable() {
  $("#ticketsTable").DataTable({
    pageLength: 10,
    order: [],
    columnDefs: [{ targets: 9, orderable: false, searchable: false }],
    language: { search: "Search results:", emptyTable: "No tickets match the current filters." },
  });
}

/* ----------------------------- Sidebar -------------------------------- */
(function initSidebar() {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  const overlay = document.getElementById("sidebar-overlay");
  if (!toggle || !sidebar) return;

  const close = () => {
    sidebar.classList.add("-translate-x-full");
    overlay.classList.add("hidden");
  };
  toggle.addEventListener("click", () => {
    sidebar.classList.toggle("-translate-x-full");
    overlay.classList.toggle("hidden");
  });
  overlay.addEventListener("click", close);
})();

/* --------------------------- Chart.js pie ----------------------------- */
function initCategoryChart() {
  const canvas = document.getElementById("categoryChart");
  if (!canvas || !window.__CHART_DATA__) return;

  const palette = ["#0a1020", "#2563eb", "#0ea5e9", "#14b8a6", "#c9f24d", "#f59e0b",
                   "#ef4444", "#8b5cf6", "#64748b", "#22c55e"];

  new Chart(canvas, {
    type: "pie",
    data: {
      labels: window.__CHART_DATA__.labels,
      datasets: [{
        data: window.__CHART_DATA__.values,
        backgroundColor: palette,
        borderColor: "#ffffff",
        borderWidth: 2,
        hoverOffset: 10,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "right", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, font: { size: 11 } } },
        tooltip: { backgroundColor: "#0a1020", padding: 10, cornerRadius: 8 },
      },
      animation: { animateRotate: true, duration: 700 },
    },
  });
}
