const page = document.body.dataset.page;
let openAiKeyPromptShown = false;
let selectedRunId = null;
let lastMilwaukeeResult = null;

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
  if (status === "application_unavailable") return "warn";
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
  node.title = ready ? "OpenAI is ready" : "Click to enter a credited OpenAI API key";
  if (!openai.api_key_present && options.prompt && !openAiKeyPromptShown) {
    openAiKeyPromptShown = true;
    await promptForOpenAiKey();
  }
}

async function promptForOpenAiKey() {
  const apiKey = window.prompt("Enter a credited OpenAI API key. On Windows, it will be saved to this user's Credential Manager.");
  if (!apiKey) return;
  const result = await getJson("/settings/openai-api-key", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({api_key: apiKey}),
  });
  if (result.credential_error) {
    window.alert(`OpenAI API key is active for this session, but it could not be saved locally.\n${result.credential_error}`);
  }
  await loadHealth();
}

async function sendDiagnosticEmail() {
  await getJson("/diagnostics/email", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({run_id: selectedRunId ? Number(selectedRunId) : null, include_state_bundle: true}),
  });
  window.alert("Diagnostic email sent with state bundle.");
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
  $("#send-diagnostic-email")?.addEventListener("click", () => sendDiagnosticEmail().catch((error) => alert(error.message)));
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

function setupMilwaukeePage() {
  loadHealth().catch(console.error);
  $("#health")?.addEventListener("click", () => promptForOpenAiKey().catch((error) => alert(error.message)));
  $("#milwaukee-form")?.addEventListener("submit", runMilwaukeeMvp);
  $("#save-milwaukee-snapshot")?.addEventListener("click", saveMilwaukeeSnapshot);
}

async function runMilwaukeeMvp(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const button = event.currentTarget.querySelector("button[type='submit']");
  const status = $("#milwaukee-status");
  const payload = {
    date_from: form.get("date_from"),
    date_to: form.get("date_to"),
    max_items: Number(form.get("max_items") || 6),
    documents_per_item: Number(form.get("documents_per_item") || 1),
    include_text: Boolean(form.get("include_text")),
    include_llm: Boolean(form.get("include_llm")),
  };
  if (payload.include_llm) await loadHealth({prompt: true});
  if (button) button.disabled = true;
  if (status) status.textContent = payload.include_llm ? "Extracting contacts..." : payload.include_text ? "Extracting text..." : "Discovering evidence...";
  try {
    const result = await getJson("/milwaukee-cpc/smoke-test", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    lastMilwaukeeResult = result;
    renderMilwaukeeResults(result);
  } catch (error) {
    if (status) status.textContent = error.message;
    alert(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

function renderMilwaukeeResults(result) {
  const status = $("#milwaukee-status");
  const list = $("#milwaukee-results");
  const saveButton = $("#save-milwaukee-snapshot");
  if (saveButton) saveButton.disabled = false;
  if (status) {
    const projectText = result.project_count ? `, ${result.project_count} project group(s)` : "";
    status.textContent = `${result.event_count || 0} event(s), ${result.item_count || 0} candidate item(s)${projectText}`;
  }
  if (!list) return;
  const eventSummary = milwaukeeEventSummary(result.events || []);
  const trackingSummary = milwaukeeTrackingSummary(result.tracking_summary);
  const projects = result.projects || [];
  const cards = projects.length ? projects.map(milwaukeeProjectCard).join("") : (result.items || []).map(milwaukeeItemCard).join("");
  list.innerHTML = eventSummary + trackingSummary + (cards || '<p class="muted">No Milwaukee CPC candidate items found.</p>');
}

async function saveMilwaukeeSnapshot() {
  if (!lastMilwaukeeResult) {
    alert("Run the Milwaukee MVP before saving a snapshot.");
    return;
  }
  const result = await getJson("/milwaukee-cpc/snapshots", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({findings: lastMilwaukeeResult}),
  });
  const status = $("#milwaukee-status");
  if (status) status.textContent = `Saved ${result.filename}`;
  alert(`Saved Milwaukee snapshot:\n${result.path}`);
}

function milwaukeeEventSummary(events) {
  if (!events.length) return "";
  const rows = events.map((event) => `
    <span class="mini-pill">${escapeHtml(formatDate(event.meeting_date))} | Event ${escapeHtml(event.event_id)}</span>
  `).join("");
  return `<div class="mini-pill-row">${rows}</div>`;
}

function milwaukeeTrackingSummary(summary) {
  if (!summary) return "";
  const rows = [
    ["Candidate files", summary.items],
    ["Ranked docs", summary.ranked_docs],
    ["Extracted docs", summary.extracted_docs],
    ["Contact docs", summary.contact_dig_docs],
    ["Evidence rows", summary.contact_evidence_rows],
    ["Primary contact projects", summary.primary_contact_projects],
    ["Project-team only", summary.project_team_only_projects],
    ["Verified no contact", summary.verified_no_contact_projects],
    ["External leads", summary.external_enrichment_candidates],
  ].map(([label, value]) => `<span class="mini-pill">${escapeHtml(label)}: ${Number(value || 0)}</span>`).join("");
  return `<div class="mini-pill-row">${rows}</div>`;
}

function milwaukeeItemCard(item) {
  return `
    <article class="card mvp-card">
      <div class="card-head">
        <strong>${escapeHtml(formatDate(item.meeting_date))} | File ${escapeHtml(item.city_file || item.matter_id)}</strong>
        <span class="status-pill">Score ${Number(item.candidate_score || 0)}</span>
      </div>
      <p class="mvp-title">${escapeHtml(item.title)}</p>
      <p class="muted">${escapeHtml(item.candidate_reason)}</p>
      ${milwaukeeDocuments(item.evidence_documents || [], "Evidence documents")}
      ${milwaukeeExtraction(item.extraction)}
    </article>
  `;
}

function milwaukeeProjectCard(project) {
  const tags = (project.tags || []).map((tag) => `<span class="mini-pill">${escapeHtml(tag)}</span>`).join("");
  const related = (project.related_files || []).map((file) => `
    <tr>
      <td>${escapeHtml(formatDate(file.meeting_date))}</td>
      <td>${escapeHtml(file.city_file || file.matter_id)}</td>
      <td>${Number(file.score || 0)}</td>
      <td>${escapeHtml(file.title || "")}</td>
    </tr>
  `).join("");
  return `
    <article class="card mvp-card">
      <div class="card-head">
        <strong>${escapeHtml(project.project_name || "Unnamed project")}</strong>
        <span class="status-pill">${project.target_project === true ? "Target" : project.target_project === false ? "Not target" : "Review"}</span>
      </div>
      <div class="fields">
        <div class="field-block">
          <h3>Project</h3>
          <p>${escapeHtml(project.project_address || "No project address")}</p>
          <p>Units: ${escapeHtml(project.unit_count ?? "")}</p>
          <p>${escapeHtml(project.building_type || "")}</p>
        </div>
        <div class="field-block">
          <h3>Decision</h3>
          <p>${escapeHtml(project.target_reason || "")}</p>
          <p>Confidence: ${Number(project.confidence || 0).toFixed(2)}</p>
        </div>
        <div class="field-block">
          <h3>Tags</h3>
          <div class="mini-pill-row">${tags || '<span class="muted">No tags</span>'}</div>
        </div>
      </div>
      ${milwaukeeExtraction(project.extraction)}
      ${milwaukeeContactSearch(project.contact_search_verification)}
      ${milwaukeeExternalEnrichment(project.external_enrichment_candidates || [])}
      ${milwaukeeDocuments(project.contact_dig_documents || [], "Contact dig documents")}
      <details class="source-attributes" open>
        <summary>Related CPC actions (${project.item_count || 0})</summary>
        <div class="table-wrap">
          <table class="mvp-evidence-table">
            <thead><tr><th>Date</th><th>File</th><th>Score</th><th>Title</th></tr></thead>
            <tbody>${related}</tbody>
          </table>
        </div>
      </details>
    </article>
  `;
}

function milwaukeeExternalEnrichment(candidates) {
  if (!candidates.length) return "";
  const rows = candidates.map((candidate) => `
    <tr>
      <td>${escapeHtml(candidate.role || "")}</td>
      <td>${escapeHtml(candidate.name || "")}</td>
      <td>${escapeHtml(candidate.company || "")}</td>
      <td>${escapeHtml(candidate.reason || "")}</td>
    </tr>
  `).join("");
  return `
    <details class="source-attributes" open>
      <summary>External enrichment candidates (${candidates.length})</summary>
      <div class="table-wrap">
        <table class="mvp-evidence-table">
          <thead><tr><th>Role</th><th>Name</th><th>Company</th><th>Reason</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>
  `;
}

function milwaukeeContactSearch(verification) {
  if (!verification) return "";
  const documents = verification.checked_documents || [];
  const rows = documents.map((document) => `
    <tr>
      <td>${escapeHtml(document.document_role || "")}</td>
      <td>${escapeHtml(document.document_name || "")}</td>
      <td>${escapeHtml(document.checked_result || "")}</td>
      <td>${Number(document.phone_signal_count || 0)}</td>
      <td>${Number(document.email_signal_count || 0)}</td>
      <td>${Number(document.city_boilerplate_phone_count || 0)}</td>
      <td>${escapeHtml(document.text_chars || "")}</td>
    </tr>
  `).join("");
  return `
    <details class="source-attributes" open>
      <summary>Contact search: ${escapeHtml(verification.status || "unknown")} (${Number(verification.checked_document_count || 0)} docs)</summary>
      <div class="table-wrap">
        <table class="mvp-evidence-table">
          <thead><tr><th>Pass</th><th>Document</th><th>Result</th><th>Phones</th><th>Emails</th><th>City phones</th><th>Text</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="7" class="muted">No checked documents.</td></tr>'}</tbody>
        </table>
      </div>
    </details>
  `;
}

function milwaukeeDocuments(documents, label) {
  if (!documents.length) return '<p class="muted">No high-signal evidence documents ranked.</p>';
  const rows = documents.map((document) => `
    <tr class="${document.selected ? "selected-evidence" : ""}">
      <td>${document.selected ? "Yes" : ""}</td>
      <td>${escapeHtml(document.name)}</td>
      <td>${Number(document.score ?? document.contact_dig_score ?? 0)}</td>
      <td>${escapeHtml(document.reason || document.contact_dig_reason || "")}</td>
      <td>${escapeHtml(document.text_chars || "")}</td>
      <td>${document.source_url ? `<a href="${escapeHtml(document.source_url)}" target="_blank" rel="noreferrer">Open</a>` : ""}</td>
    </tr>
    ${document.error ? `<tr><td></td><td colspan="5" class="fail">${escapeHtml(document.error)}</td></tr>` : ""}
  `).join("");
  return `
    <details class="source-attributes" open>
      <summary>${escapeHtml(label || "Evidence documents")} (${documents.length})</summary>
      <div class="table-wrap">
        <table class="mvp-evidence-table">
          <thead><tr><th>Use</th><th>Document</th><th>Score</th><th>Reason</th><th>Text</th><th>URL</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>
  `;
}

function milwaukeeExtraction(extraction) {
  if (!extraction) return "";
  if (extraction.error) return `<div class="notice warning"><strong>Extraction</strong>${escapeHtml(extraction.error)}</div>`;
  return `
    <div class="mvp-extraction">
      <div class="fields">
        <div class="field-block">
          <h3>Project</h3>
          <p>${escapeHtml(extraction.project_name || "No project name")}</p>
          <p>${escapeHtml(extraction.project_address || "No project address")}</p>
          <p>Units: ${escapeHtml(extraction.unit_count ?? "")}</p>
        </div>
        <div class="field-block">
          <h3>Target</h3>
          <p>${escapeHtml(extraction.target_project === true ? "Yes" : extraction.target_project === false ? "No" : "Unknown")}</p>
          <p>${escapeHtml(extraction.target_reason || "")}</p>
        </div>
        <div class="field-block">
          <h3>Review</h3>
          ${milwaukeeReviewNotes(extraction.review_notes || [])}
        </div>
      </div>
      ${milwaukeeContacts(extraction.contacts || [])}
      ${milwaukeeEvidence(extraction.evidence || [])}
    </div>
  `;
}

function milwaukeeReviewNotes(notes) {
  if (!notes.length) return '<p class="muted">No notes</p>';
  return notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("");
}

function milwaukeeContacts(contacts) {
  if (!contacts.length) return '<p class="muted">No contacts extracted.</p>';
  const rows = contacts.map((contact) => `
    <tr>
      <td>${escapeHtml(contact.role)}</td>
      <td>${escapeHtml(contact.name || "")}</td>
      <td>${escapeHtml(contact.company || "")}</td>
      <td>${escapeHtml(contact.mailing_address || "")}</td>
      <td>${escapeHtml(contact.phone || "")}</td>
      <td>${escapeHtml(contact.email || "")}</td>
      <td>${Number(contact.confidence || 0).toFixed(2)}</td>
      <td>${escapeHtml(contact.outreach_priority || "")}</td>
      <td>${escapeHtml((contact.review_flags || []).join(", "))}</td>
    </tr>
  `).join("");
  return `
    <div class="table-wrap mvp-contact-table">
      <table>
        <thead><tr><th>Role</th><th>Name</th><th>Company</th><th>Address</th><th>Phone</th><th>Email</th><th>Conf.</th><th>Priority</th><th>Flags</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function milwaukeeEvidence(evidence) {
  if (!evidence.length) return "";
  const rows = evidence.map(sourceAttributeRow).join("");
  return `
    <details class="source-attributes">
      <summary>Field evidence (${evidence.length})</summary>
      <div class="table-wrap"><table><tbody>${rows}</tbody></table></div>
    </details>
  `;
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

const REVIEW_CONTACTS = [
  ["Applicant", "applicant"],
  ["Project Contact", "project_contact"],
  ["Owner", "owner"],
];

const REVIEW_CONTACT_FIELDS = [
  ["name", "Name", "input"],
  ["company", "Company", "input"],
  ["mailing_address", "Mailing address", "textarea"],
  ["phone", "Phone", "input"],
  ["email", "Email", "input"],
];

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

function reviewEditor(row) {
  return `
    <div class="review-editor">
      ${reviewProjectSection(row)}
      ${REVIEW_CONTACTS.map(([title, prefix]) => reviewContactSection(title, prefix, row)).join("")}
    </div>
  `;
}

function reviewProjectSection(row) {
  return `
    <section class="review-section">
      <h3>Project</h3>
      <div class="review-field-grid review-project-grid">
        ${targetProjectSelect(row)}
        ${reviewField(row, "section5_description", "Section 5", "textarea")}
        ${reviewField(row, "unit_count", "Units", "input")}
      </div>
    </section>
  `;
}

function reviewContactSection(title, prefix, row) {
  const fields = REVIEW_CONTACT_FIELDS
    .map(([suffix, label, kind]) => reviewField(row, `${prefix}_${suffix}`, label, kind))
    .join("");
  return `
    <section class="review-section">
      <h3>${title}</h3>
      <div class="review-field-grid">${fields}</div>
    </section>
  `;
}

function reviewField(row, field, label, kind) {
  const value = fieldValue(row, field);
  if (kind === "textarea") {
    return `
      <label>
        ${label}
        <textarea data-review-field="${row.id}" data-field="${field}">${escapeHtml(value)}</textarea>
      </label>
    `;
  }
  return `
    <label>
      ${label}
      <input data-review-field="${row.id}" data-field="${field}" value="${escapeHtml(value)}">
    </label>
  `;
}

function targetProjectSelect(row) {
  const value = targetProjectValue(row);
  return `
    <label>
      Target
      <select data-review-field="${row.id}" data-field="target_project">
        <option value=""${value === "" ? " selected" : ""}>Unknown</option>
        <option value="true"${value === "true" ? " selected" : ""}>Yes</option>
        <option value="false"${value === "false" ? " selected" : ""}>No</option>
      </select>
    </label>
  `;
}

function fieldValue(row, field) {
  return row[field] ?? "";
}

function targetProjectValue(row) {
  if (row.target_project === true || row.target_project === 1 || row.target_project === "1") return "true";
  if (row.target_project === false || row.target_project === 0 || row.target_project === "0") return "false";
  return "";
}

function targetProjectLabel(row) {
  const value = targetProjectValue(row);
  if (value === "true") return "Yes";
  if (value === "false") return "No";
  return "Unknown";
}

function reviewActions(row) {
  return `
    <div class="review-actions">
      <input data-notes="${row.id}" placeholder="Notes" value="${escapeHtml(row.notes)}">
      <button class="secondary" data-save="${row.id}" type="button">Save changes</button>
      <button data-accept="${row.id}" type="button">Accept</button>
      <button data-reject="${row.id}" class="danger" type="button">Reject</button>
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
  const details = review ? reviewEditor(row) : applicationDetails(row);
  const actions = review ? reviewActions(row) : "";
  return `
    <article class="card ${qualityIssues(row).length ? "card-warning" : ""}">
      <div class="card-head">
        <strong>${escapeHtml(formatDate(row.meeting_date))} | ${agendaItem}</strong>
        <span class="${statusClass(row.status, row)}">${escapeHtml(row.status)}</span>
      </div>
      ${warnings}
      ${duplicates}
      ${details}
      ${rawAttributes}
      ${actions}
    </article>
  `;
}

function applicationDetails(row) {
  return `
    <div class="fields">
      ${contactBlock("Applicant", "applicant", row)}
      ${contactBlock("Project Contact", "project_contact", row)}
      ${contactBlock("Owner", "owner", row)}
    </div>
    <div class="evidence">
      <p><strong>Target:</strong> ${targetProjectLabel(row)} ${row.target_reason ? `- ${escapeHtml(row.target_reason)}` : ""}</p>
      <p><strong>Section 5:</strong> ${escapeHtml(row.section5_description)}</p>
      <p><strong>Units:</strong> ${escapeHtml(row.unit_count)}</p>
    </div>
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
  const notes = document.querySelector(`[data-notes="${id}"]`)?.value.trim();
  const corrected_fields = collectReviewFields(id);
  await getJson(`/application-extractions/${id}/review`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({status, corrected_fields, notes}),
  });
  await loadReview();
}

function collectReviewFields(id) {
  const corrected = {};
  document.querySelectorAll(`[data-review-field="${id}"][data-field]`).forEach((input) => {
    const field = input.dataset.field;
    const value = input.value.trim();
    corrected[field] = field === "target_project" ? targetProjectCorrection(value) : value;
  });
  return corrected;
}

function targetProjectCorrection(value) {
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
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
  list.querySelectorAll("[data-save]").forEach((button) => {
    button.addEventListener("click", () => submitReview(button.dataset.save, "needs_operator_review").catch(alert));
  });
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
if (page === "milwaukee") setupMilwaukeePage();
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
