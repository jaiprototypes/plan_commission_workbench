from __future__ import annotations

import os
import datetime as dt
import json
import zipfile

from fastapi.testclient import TestClient

from plan_commission_workbench import statuses
from plan_commission_workbench.api import PlanCommissionWorkbench
from plan_commission_workbench.models import AgendaClassification, AgendaSegment
from plan_commission_workbench.runtime import WorkbenchRuntime
from plan_commission_workbench.server import PACKAGE_ROOT, create_app
from plan_commission_workbench.settings import OpenAIKeyManager
from plan_commission_workbench.storage import ReviewStore


def test_ui_pages_render_without_template_errors() -> None:
    client = TestClient(create_app(start_watchdog=False))

    for path in ("/", "/agenda", "/applications", "/review", "/milwaukee"):
        response = client.get(path)

        assert response.status_code == 200
        assert "Plan Commission Workbench" in response.text


def test_milwaukee_form_uses_clear_scrape_limit_labels() -> None:
    client = TestClient(create_app(start_watchdog=False))

    response = client.get("/milwaukee")

    assert response.status_code == 200
    assert "Candidate limit" in response.text
    assert "Initial evidence docs" in response.text


def test_milwaukee_js_renders_tracking_summary() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "milwaukeeTrackingSummary" in script
    assert "Candidate files" in script
    assert "Evidence rows" in script
    assert "Primary contact projects" in script
    assert "Project-team only" in script
    assert "Verified no contact" in script
    assert "External leads" in script
    assert "milwaukeeContactSearch" in script
    assert "milwaukeeExternalEnrichment" in script


def test_applications_js_hides_rejected_rows_in_dropdown() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "rejectedApplicationsDropdown" in script
    assert "Rejected applications" in script
    assert 'row.status !== "rejected"' in script


def test_run_js_prompts_for_missing_openai_key() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "promptForOpenAiKey" in script
    assert "credited OpenAI API key" in script
    assert "/settings/openai-api-key" in script
    assert "OpenAI key required" in script


def test_run_js_can_download_state_bundle() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "downloadStateBundle" in script
    assert "/diagnostics/state-bundle" in script
    assert "download_url" in script


def test_run_ui_exposes_only_diagnostic_send_action() -> None:
    template = (PACKAGE_ROOT / "templates" / "run.html").read_text(encoding="utf-8")
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "send-diagnostic-email" in template
    assert "Send Diagnostics" in template
    assert "diagnostic-email-form" not in template
    assert 'name="delivery_method"' not in template
    assert "Sign in with Gmail" not in template
    assert "Sign in with Microsoft" not in template
    assert "Advanced SMTP" not in template
    assert "Connect Gmail" not in template
    assert "Connect Microsoft" not in template
    assert 'name="smtp_preset"' not in template
    assert 'name="smtp_password"' not in template
    assert "Google OAuth Client ID" not in template
    assert "Microsoft OAuth Client ID" not in template
    assert "smtpPresets" not in script
    assert "smtp.gmail.com" not in script
    assert "smtp.office365.com" not in script
    assert "smtp.mail.yahoo.com" not in script
    assert "smtp.mail.me.com" not in script
    assert "smtp.zoho.com" not in script
    assert "applyDiagnosticEmailPreset" not in script
    assert "connectDiagnosticEmailProvider" not in script
    assert "/settings/diagnostic-email/oauth" not in script
    assert "/diagnostics/email" in script
    assert "window.confirm" not in script
    assert "include_state_bundle: true" in script


def test_agenda_js_exposes_review_actions() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "reviewAgendaItem" in script
    assert "/agenda-items/${id}/review" in script
    assert "data-agenda-review" in script
    assert '"not_target_project"].includes(status)' in script


def test_review_js_links_items_to_agenda_rows() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "agendaItemLink" in script
    assert "/agenda?item=${encodeURIComponent(row.agenda_item_id)}" in script
    assert "data-agenda-id" in script
    assert "scrollToFocusedAgendaRow" in script


def test_agenda_table_uses_compact_description_cells() -> None:
    template = (PACKAGE_ROOT / "templates" / "agenda.html").read_text(encoding="utf-8")
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (PACKAGE_ROOT / "static" / "styles.css").read_text(encoding="utf-8")

    assert "agenda-table" in template
    assert "<th>Conf.</th>" in template
    assert "agenda-description" in script
    assert "agenda-text-box" in script
    assert ".agenda-table" in styles
    assert "table-layout: fixed" in styles
    assert "white-space: nowrap" in styles


def test_agenda_ui_can_hide_not_target_rows() -> None:
    template = (PACKAGE_ROOT / "templates" / "agenda.html").read_text(encoding="utf-8")
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "agenda-hide-not-target" in template
    assert "agendaRowsForDisplay" in script
    assert 'status === "not_target_project"' in script
    assert 'row.classification !== "not_target_project"' in script


def test_ui_formats_visible_dates_as_month_day_year() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "function formatDate" in script
    assert "${month}/${day}/${year}" in script
    assert "formatDate(row.date_from)" in script
    assert "formatDate(row.meeting_date)" in script


def test_review_cards_use_compact_professional_spacing() -> None:
    styles = (PACKAGE_ROOT / "static" / "styles.css").read_text(encoding="utf-8")

    assert ".card-head strong" in styles
    assert "font-size: 11px" in styles
    assert ".review-editor" in styles
    assert ".review-field-grid textarea" in styles
    assert "min-height: 56px" in styles


def test_review_ui_uses_editable_fields_for_corrections() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "reviewEditor" in script
    assert "REVIEW_CONTACT_FIELDS" in script
    assert "targetProjectSelect" in script
    assert "collectReviewFields" in script
    assert "data-review-field" in script
    assert "data-save" in script
    assert "data-corrections" not in script


def test_review_js_downloads_workbook_exports() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "downloadExport(result.id)" in script
    assert "/exports/${exportId}/download" in script
    assert "Your browser will download the workbook" in script


def test_state_bundle_endpoint_returns_zip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PCW_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app(start_watchdog=False))

    created = client.post("/diagnostics/state-bundle")
    assert created.status_code == 200
    payload = created.json()
    downloaded = client.get(payload["download_url"])

    assert downloaded.status_code == 200
    zip_path = tmp_path / "state.zip"
    zip_path.write_bytes(downloaded.content)
    with zipfile.ZipFile(zip_path) as archive:
        assert "workbench.db" in archive.namelist()
        assert "manifest.json" in archive.namelist()


def test_milwaukee_snapshot_endpoint_writes_diagnostics_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PCW_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app(start_watchdog=False))

    response = client.post("/milwaukee-cpc/snapshots", json={"findings": {"item_count": 1}})

    assert response.status_code == 200
    path = tmp_path / "data" / "diagnostics" / response.json()["filename"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "milwaukee_cpc_mvp"
    assert payload["findings"]["item_count"] == 1


def test_agenda_review_endpoint_updates_classification(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PCW_DATA_DIR", str(data_dir))
    runtime = WorkbenchRuntime(data_dir=data_dir)
    store = ReviewStore(runtime.db_path)
    store.initialize()
    run_id = store.create_run(dt.date(2026, 5, 11), dt.date(2026, 5, 11), None)
    source_id = store.upsert_source_item(
        run_id=run_id,
        source_kind="agenda",
        event_id="28718",
        file_id=None,
        attachment_id=None,
        source_url="https://example.test/agenda.pdf",
        content_hash="agenda-hash",
        processing_status=statuses.NEEDS_AGENDA_REVIEW,
    )
    agenda_id = store.upsert_agenda_item(
        run_id,
        source_id,
        AgendaSegment("28718", "100058", "91511", dt.date(2026, 5, 11), "493-unit multi-family dwelling"),
        AgendaClassification("100058", statuses.NEEDS_AGENDA_REVIEW, 0, "Needs review", "493-unit"),
    )
    client = TestClient(create_app(start_watchdog=False))

    response = client.patch(f"/agenda-items/{agenda_id}/review", json={"classification": statuses.AGENDA_HIT})

    assert response.status_code == 200
    assert response.json()["classification"] == statuses.AGENDA_HIT


def test_server_can_set_openai_key_for_current_process(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(start_watchdog=False))

    assert client.get("/health").json()["openai"]["api_key_present"] is False
    blocked = client.post(
        "/runs/madison",
        json={"date_from": "2026-06-01", "date_to": "2026-06-01"},
    )
    assert blocked.status_code == 400
    response = client.post("/settings/openai-api-key", json={"api_key": "sk-test"})

    assert response.status_code == 200
    assert response.json()["api_key_present"] is True
    assert os.getenv("OPENAI_API_KEY") == "sk-test"


def test_server_persists_openai_key_through_key_manager(monkeypatch, tmp_path) -> None:
    class FakeCredentialStore:
        """Purpose: prove the settings endpoint saves through the manager."""

        def __init__(self) -> None:
            self.written_secret = None

        def is_available(self) -> bool:
            return True

        def read_secret(self) -> str | None:
            return None

        def write_secret(self, secret: str) -> None:
            self.written_secret = secret

        def delete_secret(self) -> bool:
            return False

    store = FakeCredentialStore()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PCW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "plan_commission_workbench.api.OpenAIKeyManager",
        lambda: OpenAIKeyManager(credential_store=store),
    )
    client = TestClient(create_app(start_watchdog=False))

    response = client.post("/settings/openai-api-key", json={"api_key": "sk-test"})

    assert response.status_code == 200
    assert response.json()["credential_saved"] is True
    assert store.written_secret == "sk-test"


def test_server_diagnostic_email_and_secret_endpoints(monkeypatch, tmp_path) -> None:
    class FakeDiagnosticEmailService:
        """Purpose: prove API routes delegate to the diagnostic service."""

        def __init__(self) -> None:
            self.configured_payload = None
            self.sent_report = None

        def status(self):
            return {"recipient": "support@example.com", "configured": True, "credential_saved": True}

        def configure(self, payload):
            self.configured_payload = payload
            return {"configured": True, "credential_saved": True, **payload}

        def send_test_email(self):
            return {"sent": True}

        def send_run_report(self, run_id, *, include_state_bundle=False, state_bundle_path=None):
            self.sent_report = (run_id, include_state_bundle, state_bundle_path)
            return {"sent": True, "attached_state_bundle": bool(state_bundle_path)}

        def clear_email_credential(self):
            return {"credential_deleted": True, "configured": False, "credential_saved": False}

        def send_failure_report_if_enabled(self, _run_id):
            return None

    fake = FakeDiagnosticEmailService()
    monkeypatch.setenv("PCW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("plan_commission_workbench.api.DiagnosticEmailService", lambda **_kwargs: fake)
    client = TestClient(create_app(start_watchdog=False))

    settings = client.post(
        "/settings/diagnostic-email",
        json={"recipient": "support@example.com", "smtp_host": "smtp.example.com", "smtp_username": "mailer@example.com"},
    )
    test_email = client.post("/settings/diagnostic-email/test")
    removed_oauth_start = client.post("/settings/diagnostic-email/oauth/gmail/start")
    removed_oauth_callback = client.get(
        "/settings/diagnostic-email/oauth/gmail/callback?state=oauth-state&code=oauth-code"
    )
    manual = client.post("/diagnostics/email", json={"run_id": None})
    cleared = client.delete("/settings/diagnostic-email/credential")
    cleared_all = client.delete("/settings/secrets")

    assert settings.status_code == 200
    assert fake.configured_payload == {"enabled": False}
    assert test_email.json()["sent"] is True
    assert removed_oauth_start.status_code == 404
    assert removed_oauth_callback.status_code == 404
    assert manual.json()["sent"] is True
    assert manual.json()["attached_state_bundle"] is True
    assert fake.sent_report[1] is True
    assert fake.sent_report[2].suffix == ".zip"
    assert cleared.json()["credential_deleted"] is True
    assert cleared_all.json()["diagnostic_email"]["credential_deleted"] is True


def test_run_endpoint_spawns_child_worker(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PCW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = []

    def fake_start(self, run_id, request):
        """Purpose: prove the web endpoint delegates scrape work out of process."""

        calls.append((run_id, request.date_from.isoformat(), request.date_to.isoformat()))
        return {"run_id": run_id, "status": statuses.RUNNING, "worker_pid": 4321}

    monkeypatch.setattr(PlanCommissionWorkbench, "start_madison_run_worker", fake_start)
    client = TestClient(create_app(start_watchdog=False))
    response = client.post("/runs/madison", json={"date_from": "2026-06-01", "date_to": "2026-06-02"})

    assert response.status_code == 200
    assert response.json()["worker_pid"] == 4321
    assert calls == [(1, "2026-06-01", "2026-06-02")]
