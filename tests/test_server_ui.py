from __future__ import annotations

import os
import datetime as dt
import zipfile

from fastapi.testclient import TestClient

from plan_commission_workbench import statuses
from plan_commission_workbench.api import PlanCommissionWorkbench
from plan_commission_workbench.models import AgendaClassification, AgendaSegment
from plan_commission_workbench.runtime import WorkbenchRuntime
from plan_commission_workbench.server import PACKAGE_ROOT, create_app
from plan_commission_workbench.storage import ReviewStore


def test_ui_pages_render_without_template_errors() -> None:
    client = TestClient(create_app(start_watchdog=False))

    for path in ("/", "/agenda", "/applications", "/review"):
        response = client.get(path)

        assert response.status_code == 200
        assert "Plan Commission Workbench" in response.text


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


def test_agenda_js_exposes_review_actions() -> None:
    script = (PACKAGE_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "reviewAgendaItem" in script
    assert "/agenda-items/${id}/review" in script
    assert "data-agenda-review" in script


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
