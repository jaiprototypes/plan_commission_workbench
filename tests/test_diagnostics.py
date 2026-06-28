from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

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


def email_payload(**overrides):
    payload = {
        "recipient": "support@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "mailer@example.com",
        "smtp_password": "smtp-secret",
        "sender": "mailer@example.com",
        "use_ssl": False,
        "use_starttls": True,
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def test_diagnostic_email_settings_store_secret_outside_json(tmp_path) -> None:
    service, _store, credential_store, _sender = make_service(tmp_path)

    status = service.configure(email_payload())
    settings_json = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")

    assert status["configured"] is True
    assert credential_store.secret == "smtp-secret"
    assert "smtp-secret" not in settings_json


def test_diagnostic_test_email_uses_stored_credential(tmp_path) -> None:
    service, _store, _credential_store, sender = make_service(tmp_path)
    service.configure(email_payload())

    result = service.send_test_email()

    assert result["sent"] is True
    assert sender.messages[0]["recipient"] == "support@example.com"
    assert sender.messages[0]["password"] == "smtp-secret"
    assert "smtp-secret" not in sender.messages[0]["body"]


def test_manual_run_report_includes_run_context_without_secret(tmp_path) -> None:
    service, store, _credential_store, sender = make_service(tmp_path)
    service.configure(email_payload())
    run_id = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    store.log_event(run_id, "failed_application_download", "legistar", "agenda_item:1", "broken link")

    result = service.send_run_report(run_id)
    body = json.loads(sender.messages[0]["body"])

    assert result["sent"] is True
    assert body["run"]["id"] == run_id
    assert body["recent_events"][-1]["message"] == "broken link"
    assert "smtp-secret" not in sender.messages[0]["body"]


def test_clear_diagnostic_email_credential(tmp_path) -> None:
    service, _store, credential_store, _sender = make_service(tmp_path)
    service.configure(email_payload())

    result = service.clear_email_credential()

    assert result["credential_deleted"] is True
    assert credential_store.deleted is True
    assert service.status()["credential_saved"] is False


def test_automatic_failure_email_deduplicates_same_failure(tmp_path) -> None:
    service, store, _credential_store, sender = make_service(tmp_path)
    service.configure(email_payload(enabled=True))
    first_run = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    second_run = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 1), None)
    store.fail_run_from_exception(first_run, statuses.FAILED_APPLICATION_DOWNLOAD, RuntimeError("same broken link"))
    store.fail_run_from_exception(second_run, statuses.FAILED_APPLICATION_DOWNLOAD, RuntimeError("same broken link"))

    service.send_failure_report_if_enabled(first_run)
    service.send_failure_report_if_enabled(second_run)

    assert len(sender.messages) == 1
    second_events = store.list_run_events(second_run)
    assert second_events[-1]["stage"] == "diagnostic_email_duplicate_skipped"
