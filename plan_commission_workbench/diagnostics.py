"""Diagnostic email settings, reports, and SMTP delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from email.message import EmailMessage
import hashlib
from importlib import metadata
import json
from pathlib import Path
import smtplib
import ssl
from typing import Any
import uuid

from .settings import CredentialStore, WindowsCredentialStore
from .storage import ReviewStore


EMAIL_PASSWORD_TARGET = "PlanCommissionWorkbench/DiagnosticEmailPassword"
SETTINGS_FILENAME = "settings.json"


class DiagnosticEmailError(RuntimeError):
    """Purpose: report diagnostic email failures without leaking credentials."""


@dataclass
class DiagnosticEmailSettings:
    """Purpose: persist non-secret diagnostic email configuration."""

    recipient: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    sender: str = ""
    use_ssl: bool = False
    use_starttls: bool = True
    enabled: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DiagnosticEmailSettings":
        """Purpose: tolerate older or partially-written local settings files."""

        raw = raw or {}
        return cls(
            recipient=str(raw.get("recipient") or ""),
            smtp_host=str(raw.get("smtp_host") or ""),
            smtp_port=int(raw.get("smtp_port") or 587),
            smtp_username=str(raw.get("smtp_username") or ""),
            sender=str(raw.get("sender") or ""),
            use_ssl=bool(raw.get("use_ssl", False)),
            use_starttls=bool(raw.get("use_starttls", True)),
            enabled=bool(raw.get("enabled", False)),
        )

    def public_dict(self, *, credential_saved: bool) -> dict[str, Any]:
        """Purpose: expose email settings without the SMTP secret."""

        return {
            **asdict(self),
            "credential_saved": credential_saved,
            "configured": self.is_configured(credential_saved),
        }

    def is_configured(self, credential_saved: bool) -> bool:
        """Purpose: decide whether SMTP sending has enough configuration."""

        return bool(self.recipient and self.smtp_host and self.smtp_username and credential_saved)

    def from_address(self) -> str:
        """Purpose: choose a usable sender address from configured fields."""

        return self.sender or self.smtp_username


class LocalSettingsStore:
    """Purpose: persist non-secret app settings under the user data folder."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / SETTINGS_FILENAME

    def email_settings(self) -> DiagnosticEmailSettings:
        """Purpose: read diagnostic email configuration."""

        return DiagnosticEmailSettings.from_dict(self._read().get("diagnostic_email"))

    def save_email_settings(self, settings: DiagnosticEmailSettings) -> None:
        """Purpose: write diagnostic email configuration without credentials."""

        data = self._read()
        data["diagnostic_email"] = asdict(settings)
        self._write(data)

    def support_install_id(self) -> str:
        """Purpose: identify one install without using user or machine names."""

        data = self._read()
        install_id = str(data.get("support_install_id") or "")
        if install_id:
            return install_id
        install_id = uuid.uuid4().hex
        data["support_install_id"] = install_id
        self._write(data)
        return install_id

    def last_failure_email_key(self) -> str:
        """Purpose: read the last automatic failure notification fingerprint."""

        return str(self._read().get("last_failure_email_key") or "")

    def save_last_failure_email_key(self, key: str) -> None:
        """Purpose: persist the last automatic failure notification fingerprint."""

        data = self._read()
        data["last_failure_email_key"] = key
        self._write(data)

    def _read(self) -> dict[str, Any]:
        """Purpose: load local settings defensively."""

        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        """Purpose: atomically replace the settings file."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)


class SmtpDiagnosticEmailSender:
    """Purpose: send diagnostics through a configured SMTP email service."""

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def send(
        self,
        *,
        settings: DiagnosticEmailSettings,
        password: str,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
    ) -> None:
        """Purpose: send one diagnostic message with optional local files."""

        message = EmailMessage()
        message["From"] = settings.from_address()
        message["To"] = settings.recipient
        message["Subject"] = subject
        message.set_content(body)
        for path in attachments or []:
            self._attach_file(message, path)
        try:
            if settings.use_ssl:
                with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=self.timeout_seconds) as smtp:
                    self._send_with_login(smtp, settings, password, message)
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=self.timeout_seconds) as smtp:
                    if settings.use_starttls:
                        smtp.starttls(context=ssl.create_default_context())
                    self._send_with_login(smtp, settings, password, message)
        except Exception as exc:
            raise DiagnosticEmailError(f"Diagnostic email send failed: {exc}") from exc

    def _send_with_login(self, smtp, settings: DiagnosticEmailSettings, password: str, message: EmailMessage) -> None:
        """Purpose: authenticate only when configured, then send."""

        if settings.smtp_username:
            smtp.login(settings.smtp_username, password)
        smtp.send_message(message)

    def _attach_file(self, message: EmailMessage, path: Path) -> None:
        """Purpose: attach one diagnostics artifact by filename."""

        payload = path.read_bytes()
        if path.suffix.lower() == ".zip":
            message.add_attachment(payload, maintype="application", subtype="zip", filename=path.name)
            return
        message.add_attachment(payload, maintype="application", subtype="octet-stream", filename=path.name)


class DiagnosticEmailService:
    """Purpose: coordinate diagnostic settings, reports, and delivery."""

    def __init__(
        self,
        *,
        data_dir: Path,
        server_log_path: Path,
        server_error_log_path: Path,
        run_log_dir: Path,
        store: ReviewStore,
        credential_store: CredentialStore | None = None,
        sender: SmtpDiagnosticEmailSender | None = None,
    ) -> None:
        self.settings_store = LocalSettingsStore(data_dir)
        self.server_log_path = server_log_path
        self.server_error_log_path = server_error_log_path
        self.run_log_dir = run_log_dir
        self.store = store
        self.credential_store = credential_store or WindowsCredentialStore(EMAIL_PASSWORD_TARGET)
        self.sender = sender or SmtpDiagnosticEmailSender()

    def status(self) -> dict[str, Any]:
        """Purpose: expose diagnostic email readiness without secrets."""

        return {
            "support_install_id": self.settings_store.support_install_id(),
            **self.settings_store.email_settings().public_dict(credential_saved=self.email_credential_saved()),
        }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Purpose: save email settings and optional credential."""

        settings = DiagnosticEmailSettings.from_dict(payload)
        self.settings_store.save_email_settings(settings)
        password = str(payload.get("smtp_password") or "")
        credential_error = None
        if password:
            credential_error = self._save_password(password)
        result = self.status()
        if credential_error:
            result["credential_error"] = credential_error
        return result

    def clear_email_credential(self) -> dict[str, Any]:
        """Purpose: remove the stored SMTP credential only."""

        try:
            deleted = self.credential_store.delete_secret()
        except Exception as exc:
            return {"credential_deleted": False, "credential_error": str(exc), **self.status()}
        return {"credential_deleted": deleted, **self.status()}

    def email_credential_saved(self) -> bool:
        """Purpose: report whether an SMTP secret is available."""

        try:
            return bool(self.credential_store.read_secret())
        except Exception:
            return False

    def send_test_email(self) -> dict[str, Any]:
        """Purpose: validate SMTP settings with a small message."""

        settings, password = self._configured_settings()
        subject = self._subject("test", None)
        body = json.dumps(
            {
                "kind": "diagnostic_email_test",
                "support_install_id": self.settings_store.support_install_id(),
                "app_version": self._app_version(),
            },
            indent=2,
        )
        self.sender.send(settings=settings, password=password, subject=subject, body=body)
        return {"sent": True, **self.status()}

    def send_run_report(
        self,
        run_id: int | None,
        *,
        include_state_bundle: bool = False,
        state_bundle_path: Path | None = None,
    ) -> dict[str, Any]:
        """Purpose: send a manual diagnostic report and optional bundle."""

        settings, password = self._configured_settings()
        report = self.build_report(run_id)
        attachments = [state_bundle_path] if include_state_bundle and state_bundle_path else []
        self.sender.send(
            settings=settings,
            password=password,
            subject=self._subject("manual", run_id),
            body=json.dumps(report, indent=2, default=str),
            attachments=attachments,
        )
        return {"sent": True, "attached_state_bundle": bool(attachments), "report": report}

    def send_failure_report_if_enabled(self, run_id: int) -> None:
        """Purpose: best-effort automatic report without blocking run cleanup."""

        settings = self.settings_store.email_settings()
        if not settings.enabled:
            return
        failure_key = self._failure_key(run_id)
        if failure_key and failure_key == self.settings_store.last_failure_email_key():
            self.store.log_event(run_id, "diagnostic_email_duplicate_skipped", "diagnostics", None, "Skipped duplicate diagnostic email")
            return
        try:
            settings, password = self._configured_settings()
            report = self.build_report(run_id)
            self.sender.send(
                settings=settings,
                password=password,
                subject=self._subject("failure", run_id),
                body=json.dumps(report, indent=2, default=str),
            )
            if failure_key:
                self.settings_store.save_last_failure_email_key(failure_key)
            self.store.log_event(run_id, "diagnostic_email_sent", "diagnostics", None, "Sent automatic diagnostic email")
        except Exception as exc:
            self.store.log_event(run_id, "diagnostic_email_failed", "diagnostics", None, str(exc))

    def build_report(self, run_id: int | None) -> dict[str, Any]:
        """Purpose: build a compact redacted diagnostic report."""

        run = self.store.get_run(run_id) if run_id else None
        events = self.store.list_run_events(run_id)[-80:] if run_id else []
        return {
            "kind": "plan_commission_workbench_diagnostic",
            "support_install_id": self.settings_store.support_install_id(),
            "app_version": self._app_version(),
            "run": run,
            "recent_events": events,
            "source_items": self._source_items(run_id),
            "log_tails": {
                "server.log": self._tail_file(self.server_log_path),
                "server.err.log": self._tail_file(self.server_error_log_path),
                **self._run_log_tails(run_id),
            },
        }

    def _configured_settings(self) -> tuple[DiagnosticEmailSettings, str]:
        """Purpose: load validated settings and the stored SMTP secret."""

        settings = self.settings_store.email_settings()
        password = ""
        try:
            password = self.credential_store.read_secret() or ""
        except Exception as exc:
            raise DiagnosticEmailError(f"Could not read diagnostic email credential: {exc}") from exc
        if not settings.is_configured(bool(password)):
            raise DiagnosticEmailError("Diagnostic email settings are incomplete")
        return settings, password

    def _save_password(self, password: str) -> str | None:
        """Purpose: store SMTP credential without interrupting settings save."""

        if not self.credential_store.is_available():
            return "Windows Credential Manager is not available on this platform"
        try:
            self.credential_store.write_secret(password)
        except Exception as exc:
            return str(exc)
        return None

    def _subject(self, kind: str, run_id: int | None) -> str:
        """Purpose: keep diagnostic email grouping deterministic."""

        run_part = f" run {run_id}" if run_id else ""
        return f"[PCW diagnostics] {kind}{run_part} {self._app_version()}"

    def _failure_key(self, run_id: int) -> str:
        """Purpose: group repeated automatic reports for the same failure."""

        run = self.store.get_run(run_id)
        if not run:
            return ""
        basis = "|".join(
            [
                self._app_version(),
                str(run.get("status") or ""),
                str(run.get("heartbeat_source") or ""),
                str(run.get("last_error") or ""),
            ]
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def _source_items(self, run_id: int | None) -> list[dict[str, Any]]:
        """Purpose: include source rows for the active failure context."""

        if not run_id:
            return []
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM source_items
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 40
                """,
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _tail_file(self, path: Path, line_count: int = 80) -> str:
        """Purpose: include bounded logs without moving large files."""

        try:
            if not path.exists():
                return ""
            return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-line_count:])
        except OSError as exc:
            return f"Could not read {path.name}: {exc}"

    def _run_log_tails(self, run_id: int | None) -> dict[str, str]:
        """Purpose: include bounded child-worker logs for one run."""

        if not run_id:
            return {}
        return {
            f"run_logs/run_{run_id}.log": self._tail_file(self.run_log_dir / f"run_{run_id}.log"),
            f"run_logs/run_{run_id}.err.log": self._tail_file(self.run_log_dir / f"run_{run_id}.err.log"),
        }

    def _app_version(self) -> str:
        """Purpose: report the installed package version when available."""

        try:
            return metadata.version("plan-commission-workbench")
        except metadata.PackageNotFoundError:
            return "0.0.0"
