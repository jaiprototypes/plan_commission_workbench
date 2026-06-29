from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from plan_commission_workbench.diagnostics import DiagnosticEmailService
from plan_commission_workbench.runtime import WorkbenchRuntime
from plan_commission_workbench import statuses
from plan_commission_workbench.storage import ReviewStore


class FakeCredentialStore:
    """Purpose: avoid touching Windows Credential Manager in diagnostics tests."""

    def __init__(self) -> None:
        self.secret: str | None = None
        self.deleted = False

    def is_available(self) -> bool:
        return True

    def read_secret(self) -> str | None:
        return self.secret

    def write_secret(self, secret: str) -> None:
        self.secret = secret

    def delete_secret(self) -> bool:
        deleted = self.secret is not None
        self.secret = None
        self.deleted = True
        return deleted


class FakeEmailSender:
    """Purpose: capture outbound diagnostic messages without SMTP."""

    def __init__(self) -> None:
        self.messages = []

    def send(self, *, settings, password, subject, body, attachments=None) -> None:
        self.messages.append(
            {
                "recipient": settings.recipient,
                "password": password,
                "subject": subject,
                "body": body,
                "attachments": attachments or [],
            }
        )


def make_service(tmp_path: Path) -> tuple[DiagnosticEmailService, ReviewStore, FakeCredentialStore, FakeEmailSender]:
    runtime = WorkbenchRuntime(project_root=tmp_path, data_dir=tmp_path / "data")
    runtime.setup()
    store = ReviewStore(runtime.db_path)
    store.initialize()
    credential_store = FakeCredentialStore()
    sender = FakeEmailSender()
    service = DiagnosticEmailService(
        data_dir=runtime.data_dir,
        server_log_path=runtime.server_log_path,
        server_error_log_path=runtime.server_error_log_path,
        run_log_dir=runtime.run_log_dir,
        store=store,
        credential_store=credential_store,
        sender=sender,
    )
    return service, store, credential_store, sender


@pytest.fixture(autouse=True)
def baked_diagnostic_email_defaults(monkeypatch):
    """Purpose: simulate the release workflow's baked diagnostic sender."""

    monkeypatch.setenv("PCW_DIAGNOSTIC_EMAIL_RECIPIENT", "support@example.com")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_PORT", "587")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_USERNAME", "mailer@example.com")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_SENDER", "mailer@example.com")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_USE_SSL", "false")
    monkeypatch.setenv("PCW_DIAGNOSTIC_SMTP_USE_STARTTLS", "true")
    monkeypatch.setenv("PCW_DIAGNOSTIC_AUTO_EMAIL_FAILURES", "false")


def test_diagnostic_email_settings_store_secret_outside_json(tmp_path) -> None:
    service, _store, credential_store, _sender = make_service(tmp_path)

    status = service.configure({"enabled": True})
    settings_json = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")

    assert status["configured"] is True
    assert status["built_in_credential"] is True
    assert credential_store.secret is None
    assert "smtp-secret" not in settings_json


def test_diagnostic_test_email_uses_baked_credential(tmp_path) -> None:
    service, _store, _credential_store, sender = make_service(tmp_path)

    result = service.send_test_email()

    assert result["sent"] is True
    assert sender.messages[0]["recipient"] == "support@example.com"
    assert sender.messages[0]["password"] == "smtp-secret"
    assert "smtp-secret" not in sender.messages[0]["body"]


def test_manual_run_report_includes_run_context_without_secret(tmp_path) -> None:
    service, store, _credential_store, sender = make_service(tmp_path)
    run_id = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    store.log_event(run_id, "failed_application_download", "legistar", "agenda_item:1", "broken link")

    result = service.send_run_report(run_id)
    body = sender.messages[0]["body"]

    assert result["sent"] is True
    assert f"Run ID: {run_id}" in body
    assert "broken link" in body
    assert not body.lstrip().startswith("{")
    assert "smtp-secret" not in sender.messages[0]["body"]


def test_manual_run_report_attaches_state_bundle_without_json_body(tmp_path) -> None:
    service, store, _credential_store, sender = make_service(tmp_path)
    run_id = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    bundle_path = tmp_path / "pcw_state_bundle_test.zip"
    bundle_path.write_bytes(b"zip-bytes")

    result = service.send_run_report(run_id, include_state_bundle=True, state_bundle_path=bundle_path)
    message = sender.messages[0]

    assert result["attached_state_bundle"] is True
    assert message["attachments"] == [bundle_path]
    assert f"Attached state bundle: {bundle_path.name}" in message["body"]
    assert not message["body"].lstrip().startswith("{")


def test_clear_diagnostic_email_credential(tmp_path) -> None:
    service, _store, credential_store, _sender = make_service(tmp_path)
    credential_store.write_secret("legacy-local-secret")

    result = service.clear_email_credential()

    assert result["credential_deleted"] is True
    assert credential_store.deleted is True
    assert service.status()["credential_saved"] is True
    assert service.status()["built_in_credential"] is True


def test_automatic_failure_email_deduplicates_same_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PCW_DIAGNOSTIC_AUTO_EMAIL_FAILURES", "true")
    service, store, _credential_store, sender = make_service(tmp_path)
    first_run = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    second_run = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    store.fail_run_from_exception(first_run, statuses.FAILED_APPLICATION_DOWNLOAD, RuntimeError("same broken link"))
    store.fail_run_from_exception(second_run, statuses.FAILED_APPLICATION_DOWNLOAD, RuntimeError("same broken link"))

    service.send_failure_report_if_enabled(first_run)
    service.send_failure_report_if_enabled(second_run)

    assert len(sender.messages) == 1
    second_events = store.list_run_events(second_run)
    assert second_events[-1]["stage"] == "diagnostic_email_duplicate_skipped"
