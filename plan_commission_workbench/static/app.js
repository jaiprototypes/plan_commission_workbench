const page = document.body.dataset.page;
let openAiKeyPromptShown = false;
let selectedRunId = null;

function $(selector) {
  return document.querySelector(selector);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  // Purpose: keep API dates ISO while presenting dates in US desktop format.
  const match = String(value ?? "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return value ?? "";
  const [, year, month, day] = match;
  return `${month}/${day}/${year}`;
}

async function getJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const message = await response.text();
    try {
      throw new Error(JSON.parse(message).detail || message);
    } catch (error) {
      if (error instanceof SyntaxError) throw new Error(message);
      throw error;
    }
  }
  return response.json();
}

function qualityIssues(row) {
  return row?.quality_issues || [];
}

function statusClass(status, row = null) {
  if (status === "application_extracted" && qualityIssues(row).length) return "warn";
  if (["completed", "accepted", "application_extracted", "agenda_hit"].includes(status)) return "ok";
  if (String(status || "").startsWith("failed") || ["rejected", "not_target_project"].includes(status)) return "fail";
  return "warn";
}

async function loadHealth(options = {}) {
  const node = $("#health");
  if (!node) return;
  const health = await getJson("/health");
  const openai = health.openai || {};
  const ready = openai.api_key_present && openai.package_available;
  node.className = `status-pill ${ready ? "ok" : "warn"}`;
  node.textContent = ready ? `OpenAI ${openai.model}` : openai.api_key_present ? "OpenAI package not ready" : "OpenAI key required";
  node.title = ready ? "OpenAI is ready" : "Click to enter a credited OpenAI API key for this session";
  if (!openai.api_key_present && options.prompt && !openAiKeyPromptShown) {
    openAiKeyPromptShown = true;
    await promptForOpenAiKey();
  }
}

async function promptForOpenAiKey() {
  const apiKey = window.prompt("Enter a credited OpenAI API key for this workbench session:");
  if (!apiKey) return;
  await getJson("/settings/openai-api-key", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({api_key: apiKey}),
  });
  await loadHealth();
}

async function loadRuns() {
  const body = $("#runs-body");
  if (!body) return;
  const rows = await getJson("/runs");
  if (!selectedRunId && rows[0]) selectedRunId = String(rows[0].id);
  if (selectedRunId && !rows.some((row) => String(row.id) === selectedRunId)) {
    selectedRunId = rows[0] ? String(rows[0].id) : null;
  }
  body.innerHTML = rows.map((row) => `
    <tr class="${String(row.id) === selectedRunId ? "selected-row" : ""}">
      <td>${row.id}</td>
      <td>${escapeHtml(formatDate(row.date_from))} to ${escapeHtml(formatDate(row.date_to))}</td>
      <td class="${statusClass(row.status)}">${escapeHtml(row.status)}</td>
      <td>${row.agenda_hits || 0}/${row.agenda_total || 0}</td>
      <td>${row.applications_extracted || 0}/${row.applications_total || 0}</td>
      <td><button class="secondary" data-events="${row.id}" type="button">Log</button></td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-events]").forEach((button) => {
    button.addEventListener("click", () => loadRunEvents(button.dataset.events));
  });
  if (selectedRunId) await loadRunEvents(selectedRunId);
}

async function loadRunEvents(runId) {
  const list = $("#run-events");
  const label = $("#log-run");
  if (!list || !runId) return;
  selectedRunId = String(runId);
  let events;
  try {
    events = await getJson(`/runs/${runId}/events`);
  } catch (error) {
    renderLogRefreshError(list, error);
    return;
  }
  if (label) label.textContent = `Run ${runId}`;
  list.innerHTML = events.map((event) => `
    <div class="log-line">
      <strong>${escapeHtml(event.timestamp)}</strong>
      ${escapeHtml(event.stage)} ${escapeHtml(event.component)}
      ${event.source_identity ? `[${escapeHtml(event.source_identity)}]` : ""}
      <br>${escapeHtml(event.message)}
    </div>
  `).join("");
}

function renderLogRefreshError(list, error) {
  const message = error?.message || "Unable to refresh run log";
  const html = `
    <div class="log-line log-error" data-log-error="true">
      <strong>${new Date().toISOString()}</strong>
      log_refresh ui
      <br>${escapeHtml(message)}
    </div>
  `;
  const existing = list.querySelector("[data-log-error]");
  if (existing) {
    existing.outerHTML = html;
    return;
  }
  list.insertAdjacentHTML("beforeend", html);
}

function setupRunPage() {
  loadHealth({prompt: true}).catch((error) => alert(error.message));
  loadRuns().catch(console.error);
  $("#health")?.addEventListener("click", () => promptForOpenAiKey().catch((error) => alert(error.message)));
  $("#refresh-runs")?.addEventListener("click", () => loadRuns().catch(console.error));
  $("#download-state-bundle")?.addEventListener("click", () => downloadStateBundle().catch((error) => alert(error.message)));
  $("#run-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const payload = {
      date_from: form.get("date_from"),
      date_to: form.get("date_to"),
      request_text: form.get("request_text") || null,
    };
    const run = await getJson("/runs/madison", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    selectedRunId = String(run.run_id);
    await loadRuns();
    await loadRunEvents(run.run_id);
  });
  setInterval(() => loadRuns().catch(() => {}), 6000);
}

async function downloadStateBundle() {
  const result = await getJson("/diagnostics/state-bundle", {method: "POST"});
  window.location.href = result.download_url;
}

async function loadAgenda() {
  const body = $("#agenda-body");
  if (!body) return;
  const status = $("#agenda-status")?.value || "";
  const hideNotTarget = $("#agenda-hide-not-target")?.checked ?? true;
  const focusedAgendaId = new URLSearchParams(window.location.search).get("item");
  const rows = await getJson(`/agenda-items${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  const displayRows = agendaRowsForDisplay(rows, status, hideNotTarget);
  body.innerHTML = displayRows.map((row) => `
    <tr class="${String(row.id) === focusedAgendaId ? "selected-row agenda-focus-row" : ""}" data-agenda-id="${row.id}">
      <td>${escapeHtml(formatDate(row.meeting_date))}</td>
      <td>${escapeHtml(row.event_id)}</td>
      <td>${escapeHtml(row.city_item_id)}</td>
      <td class="${statusClass(row.classification)}">${escapeHtml(row.classification)}</td>
      <td>${Number(row.confidence || 0).toFixed(2)}</td>
      <td class="agenda-description"><div class="agenda-text-box" title="${escapeHtml(row.description)}">${escapeHtml(row.description)}</div></td>
      <td class="agenda-reason"><div class="agenda-text-box" title="${escapeHtml(row.reason)}">${escapeHtml(row.reason)}</div></td>
      <td>${agendaActions(row)}</td>
    </tr>
  `).join("");
  body.querySelectorAll("[data-agenda-review]").forEach((button) => {
    button.addEventListener("click", () => reviewAgendaItem(button.dataset.agendaReview, button.dataset.classification).catch(alert));
  });
  scrollToFocusedAgendaRow(body, focusedAgendaId);
}

function agendaRowsForDisplay(rows, status, hideNotTarget) {
  // Purpose: keep not-target rows available without letting them dominate the default agenda view.
  if (!hideNotTarget || status === "not_target_project") return rows;
  return rows.filter((row) => row.classification !== "not_target_project");
}

function scrollToFocusedAgendaRow(body, focusedAgendaId) {
  if (!focusedAgendaId) return;
  const row = Array.from(body.querySelectorAll("[data-agenda-id]"))
    .find((item) => item.dataset.agendaId === focusedAgendaId);
  if (row) row.scrollIntoView({block: "center"});
}

function agendaActions(row) {
  const buttons = [];
  if (row.classification !== "agenda_hit") {
    buttons.push(`<button class="secondary compact-button" data-agenda-review="${row.id}" data-classification="agenda_hit" type="button">Hit</button>`);
  }
  if (row.classification !== "not_target_project") {
    buttons.push(`<button class="secondary compact-button" data-agenda-review="${row.id}" data-classification="not_target_project" type="button">Not target</button>`);
  }
  if (row.classification !== "needs_agenda_review") {
    buttons.push(`<button class="secondary compact-button" data-agenda-review="${row.id}" data-classification="needs_agenda_review" type="button">Review</button>`);
  }
  return `<div class="table-actions">${buttons.join("")}</div>`;
}

async function reviewAgendaItem(id, classification) {
  await getJson(`/agenda-items/${id}/review`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({classification}),
  });
  await loadAgenda();
}

function contactBlock(title, prefix, row) {
  const fields = ["name", "company", "mailing_address", "phone", "email"]
    .map((field) => row[`${prefix}_${field}`])
    .filter((value) => String(value ?? "").trim())
    .map((value) => `<p>${escapeHtml(value)}</p>`)
    .join("");
  return `
    <div class="field-block">
      <h3>${title}</h3>
      ${fields || '<p class="muted">No populated fields</p>'}
    </div>
  `;
}

function qualityNotice(row) {
  const issues = qualityIssues(row);
  if (!issues.length) return "";
  const items = issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("");
  return `
    <div class="notice warning">
      <strong>QC review required</strong>
      <ul>${items}</ul>
    </div>
  `;
}

function duplicateNotice(row) {
  const duplicates = row.duplicate_contacts || [];
  if (!duplicates.length) return "";
  const items = duplicates.map((item) => `<li>${escapeHtml(item.message)}</li>`).join("");
  return `
    <div class="notice info">
      <strong>Saved contact match</strong>
      <ul>${items}</ul>
    </div>
  `;
}

function sourceAttributeRow(item) {
  const value = String(item.value ?? "").trim() || "No extracted value";
  const confidence = Number(item.confidence ?? 0).toFixed(2);
  return `
    <tr>
      <td>${escapeHtml(item.field_name)}</td>
      <td>${escapeHtml(value)}</td>
      <td>${escapeHtml(item.evidence_snippet)}</td>
      <td>${confidence}</td>
    </tr>
  `;
}

function sourceAttributes(evidence) {
  if (!evidence?.length) return "";
  const rows = evidence.map(sourceAttributeRow).join("");
  return `
    <details class="source-attributes">
      <summary>Raw source attributes (${evidence.length})</summary>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Field</th>
              <th>Extracted value</th>
              <th>Docling source text</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>
  `;
}

function applicationCard(row, review = false) {
  const rawAttributes = sourceAttributes(row.evidence || []);
  const warnings = qualityNotice(row);
  const duplicates = duplicateNotice(row);
  const agendaItem = review ? agendaItemLink(row) : `Item ${escapeHtml(row.city_item_id)}`;
  const actions = review ? `
    <div class="review-actions">
      <textarea data-corrections="${row.id}" placeholder='{"applicant_name":"Corrected value"}'></textarea>
      <input data-notes="${row.id}" placeholder="Notes">
      <button data-accept="${row.id}" type="button">Accept</button>
      <button data-reject="${row.id}" class="danger" type="button">Reject</button>
    </div>
  ` : "";
  return `
    <article class="card ${qualityIssues(row).length ? "card-warning" : ""}">
      <div class="card-head">
        <strong>${escapeHtml(formatDate(row.meeting_date))} | ${agendaItem}</strong>
        <span class="${statusClass(row.status, row)}">${escapeHtml(row.status)}</span>
      </div>
      ${warnings}
      ${duplicates}
      <div class="fields">
        ${contactBlock("Applicant", "applicant", row)}
        ${contactBlock("Project Contact", "project_contact", row)}
        ${contactBlock("Owner", "owner", row)}
      </div>
      <div class="evidence">
        <p><strong>Target:</strong> ${row.target_project === 0 ? "No" : row.target_project === 1 ? "Yes" : "Unknown"} ${row.target_reason ? `- ${escapeHtml(row.target_reason)}` : ""}</p>
        <p><strong>Section 5:</strong> ${escapeHtml(row.section5_description)}</p>
        <p><strong>Units:</strong> ${escapeHtml(row.unit_count)}</p>
      </div>
      ${rawAttributes}
      ${actions}
    </article>
  `;
}

function agendaItemLink(row) {
  if (!row.agenda_item_id) return `Item ${escapeHtml(row.city_item_id)}`;
  const href = `/agenda?item=${encodeURIComponent(row.agenda_item_id)}`;
  return `<a class="agenda-shortcut" href="${href}">Item ${escapeHtml(row.city_item_id)}</a>`;
}

function rejectedApplicationsDropdown(rows) {
  if (!rows.length) return "";
  const cards = rows.map((row) => applicationCard(row)).join("");
  return `
    <details class="rejected-applications">
      <summary>Rejected applications (${rows.length})</summary>
      <div class="cards nested-cards">${cards}</div>
    </details>
  `;
}

async function loadApplications() {
  const list = $("#applications-list");
  if (!list) return;
  const status = $("#application-status")?.value || "";
  const rows = await getJson(`/application-extractions${status ? `?status=${encodeURIComponent(status)}` : ""}`);
  if (!status) {
    const activeRows = rows.filter((row) => row.status !== "rejected");
    const rejectedRows = rows.filter((row) => row.status === "rejected");
    list.innerHTML = activeRows.map((row) => applicationCard(row)).join("") + rejectedApplicationsDropdown(rejectedRows);
    return;
  }
  list.innerHTML = rows.map((row) => applicationCard(row)).join("");
}

async function submitReview(id, status) {
  const correctionText = document.querySelector(`[data-corrections="${id}"]`)?.value.trim();
  const notes = document.querySelector(`[data-notes="${id}"]`)?.value.trim();
  let corrected_fields = {};
  if (correctionText) corrected_fields = JSON.parse(correctionText);
  await getJson(`/application-extractions/${id}/review`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({status, corrected_fields, notes}),
  });
  await loadReview();
}

async function loadReview() {
  const list = $("#review-list");
  if (!list) return;
  const [extractedRows, reviewRows] = await Promise.all([
    getJson("/application-extractions?status=application_extracted"),
    getJson("/application-extractions?status=needs_operator_review"),
  ]);
  const rows = [...reviewRows, ...extractedRows];
  list.innerHTML = rows.map((row) => applicationCard(row, true)).join("");
  list.querySelectorAll("[data-accept]").forEach((button) => {
    button.addEventListener("click", () => submitReview(button.dataset.accept, "accepted").catch(alert));
  });
  list.querySelectorAll("[data-reject]").forEach((button) => {
    button.addEventListener("click", () => submitReview(button.dataset.reject, "rejected").catch(alert));
  });
}

function setupExport() {
  $("#export-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const form = new FormData(event.currentTarget);
      const result = await postExport(form.get("output"));
      alert(`Prepared ${result.row_count} accepted row(s). Your browser will download the workbook.`);
      downloadExport(result.id);
    } catch (error) {
      alert(error.message);
    }
  });
  $("#label-export-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const form = new FormData(event.currentTarget);
      const result = await postExport(form.get("output"));
      const skipped = result.qc_skipped_count || 0;
      alert(`Prepared ${result.row_count} label(s). QC skipped ${skipped} contact(s).`);
      downloadExport(result.id);
    } catch (error) {
      alert(error.message);
    }
  });
}

function downloadExport(exportId) {
  window.location.href = `/exports/${exportId}/download`;
}

async function postExport(output) {
  return getJson("/exports", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({output, status: "accepted"}),
  });
}

if (page === "run") setupRunPage();
if (page === "agenda") {
  loadAgenda().catch(console.error);
  $("#agenda-status")?.addEventListener("change", () => loadAgenda().catch(console.error));
  $("#agenda-hide-not-target")?.addEventListener("change", () => loadAgenda().catch(console.error));
}
if (page === "applications") {
  loadApplications().catch(console.error);
  $("#application-status")?.addEventListener("change", () => loadApplications().catch(console.error));
}
if (page === "review") {
  loadReview().catch(console.error);
  setupExport();
}
