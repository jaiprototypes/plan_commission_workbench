from __future__ import annotations

import os

from fastapi.testclient import TestClient

from plan_commission_workbench.server import PACKAGE_ROOT, create_app


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
