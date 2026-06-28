"""Diagnostic email settings, reports, and provider delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import smtplib
import ssl
import time
from typing import Any
import uuid

from . import oauth_defaults
from .email_oauth import (
    GMAIL_DELIVERY_METHOD,
    GOOGLE_PROVIDER,
    MICROSOFT_DELIVERY_METHOD,
    MICROSOFT_PROVIDER,
    OAUTH_DELIVERY_METHODS,
    OAUTH_PROVIDER_CONFIGS,
    GmailApiDiagnosticEmailSender,
    MicrosoftGraphDiagnosticEmailSender,
    OAuthProviderClient,
    PendingOAuthRequest,
    OAuthToken,
    build_diagnostic_message,
)
from .settings import CredentialStore, WindowsCredentialStore
from .storage import ReviewStore


EMAIL_PASSWORD_TARGET = "PlanCommissionWorkbench/DiagnosticEmailPassword"
GOOGLE_OAUTH_TARGET = "PlanCommissionWorkbench/DiagnosticEmailGoogleOAuth"
MICROSOFT_OAUTH_TARGET = "PlanCommissionWorkbench/DiagnosticEmailMicrosoftOAuth"
SETTINGS_FILENAME = "settings.json"
SMTP_DELIVERY_METHOD = "smtp"
OAUTH_PENDING_TTL_SECONDS = 600


class DiagnosticEmailError(RuntimeError):
    """Purpose: report diagnostic email failures without leaking credentials."""


def _clean_delivery_method(value: str) -> str:
    """Purpose: keep older settings files on SMTP and reject bad UI values."""

    cleaned = value.strip().lower()
    if cleaned in {SMTP_DELIVERY_METHOD, GMAIL_DELIVERY_METHOD, MICROSOFT_DELIVERY_METHOD}:
        return cleaned
    return SMTP_DELIVERY_METHOD


@dataclass
class DiagnosticEmailSettings:
    """Purpose: persist non-secret diagnostic email configuration."""

    delivery_method: str = SMTP_DELIVERY_METHOD
    recipient: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    sender: str = ""
    oauth_email: str = ""
    use_ssl: bool = False
    use_starttls: bool = True
    enabled: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DiagnosticEmailSettings":
        """Purpose: tolerate older or partially-written local settings files."""

        raw = raw or {}
        return cls(
            delivery_method=_clean_delivery_method(str(raw.get("delivery_method") or SMTP_DELIVERY_METHOD)),
            recipient=str(raw.get("recipient") or ""),
            smtp_host=str(raw.get("smtp_host") or ""),
            smtp_port=int(raw.get("smtp_port") or 587),
            smtp_username=str(raw.get("smtp_username") or ""),
            sender=str(raw.get("sender") or ""),
            oauth_email=str(raw.get("oauth_email") or ""),
            use_ssl=bool(raw.get("use_ssl", False)),
            use_starttls=bool(raw.get("use_starttls", True)),
            enabled=bool(raw.get("enabled", False)),
        )

    def public_dict(
        self,
        *,
        credential_saved: bool,
        oauth_token_saved: bool,
        oauth_client_configured: bool,
    ) -> dict[str, Any]:
        """Purpose: expose email settings without SMTP or OAuth secrets."""

        return {
            **asdict(self),
            "credential_saved": credential_saved,
            "oauth_token_saved": oauth_token_saved,
            "oauth_client_configured": oauth_client_configured,
            "configured": self.is_configured(
                credential_saved=credential_saved,
                oauth_token_saved=oauth_token_saved,
                oauth_client_configured=oauth_client_configured,
            ),
        }

    def is_configured(
        self,
        *,
        credential_saved: bool,
        oauth_token_saved: bool = False,
        oauth_client_configured: bool = False,
    ) -> bool:
        """Purpose: decide whether the selected provider can send."""

        if self.delivery_method == SMTP_DELIVERY_METHOD:
            return bool(self.recipient and self.smtp_host and self.smtp_username and credential_saved)
        return bool(self.recipient and self.from_address() and oauth_client_configured and oauth_token_saved)

    def from_address(self) -> str:
        """Purpose: choose a usable sender address from configured fields."""

        return self.sender or self.smtp_username or self.oauth_email


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

        message = build_diagnostic_message(settings, subject, body, attachments)
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
        google_oauth_store: CredentialStore | None = None,
        microsoft_oauth_store: CredentialStore | None = None,
        gmail_sender: GmailApiDiagnosticEmailSender | None = None,
        microsoft_sender: MicrosoftGraphDiagnosticEmailSender | None = None,
        oauth_http=None,
    ) -> None:
        self.settings_store = LocalSettingsStore(data_dir)
        self.server_log_path = server_log_path
        self.server_error_log_path = server_error_log_path
        self.run_log_dir = run_log_dir
        self.store = store
        self.credential_store = credential_store or WindowsCredentialStore(EMAIL_PASSWORD_TARGET)
        self.sender = sender or SmtpDiagnosticEmailSender()
        self.oauth_stores = {
            GOOGLE_PROVIDER: google_oauth_store or WindowsCredentialStore(GOOGLE_OAUTH_TARGET),
            MICROSOFT_PROVIDER: microsoft_oauth_store or WindowsCredentialStore(MICROSOFT_OAUTH_TARGET),
        }
        self.oauth_clients = {
            provider: OAuthProviderClient(config, http=oauth_http)
            for provider, config in OAUTH_PROVIDER_CONFIGS.items()
        }
        self.oauth_senders = {
            GOOGLE_PROVIDER: gmail_sender or GmailApiDiagnosticEmailSender(),
            MICROSOFT_PROVIDER: microsoft_sender or MicrosoftGraphDiagnosticEmailSender(),
        }
        self.pending_oauth: dict[str, PendingOAuthRequest] = {}

    def status(self) -> dict[str, Any]:
        """Purpose: expose diagnostic email readiness without secrets."""

        settings = self.settings_store.email_settings()
        provider = self._provider_for_settings(settings)
        oauth_token_saved = self.oauth_token_saved(provider) if provider else False
        oauth_client_configured = self.oauth_client_configured(provider, settings) if provider else False
        return {
            "support_install_id": self.settings_store.support_install_id(),
            "google_oauth_client_configured": self.oauth_client_configured(GOOGLE_PROVIDER, settings),
            "microsoft_oauth_client_configured": self.oauth_client_configured(MICROSOFT_PROVIDER, settings),
            **settings.public_dict(
                credential_saved=self.email_credential_saved(),
                oauth_token_saved=oauth_token_saved,
                oauth_client_configured=oauth_client_configured,
            ),
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
        """Purpose: remove stored SMTP and OAuth diagnostic email secrets."""

        deleted = False
        errors = []
        try:
            deleted = self.credential_store.delete_secret() or deleted
        except Exception as exc:
            errors.append(str(exc))
        for store in self.oauth_stores.values():
            try:
                deleted = store.delete_secret() or deleted
            except Exception as exc:
                errors.append(str(exc))
        result = {"credential_deleted": deleted, **self.status()}
        if errors:
            result["credential_error"] = "; ".join(errors)
        return result

    def email_credential_saved(self) -> bool:
        """Purpose: report whether an SMTP secret is available."""

        try:
            return bool(self.credential_store.read_secret())
        except Exception:
            return False

    def oauth_token_saved(self, provider: str | None) -> bool:
        """Purpose: report whether a provider token is saved locally."""

        if not provider:
            return False
        store = self.oauth_stores.get(provider)
        if not store:
            return False
        try:
            return bool(store.read_secret())
        except Exception:
            return False

    def oauth_client_configured(self, provider: str | None, settings: DiagnosticEmailSettings | None = None) -> bool:
        """Purpose: report whether this build/settings can start OAuth."""

        if not provider:
            return False
        return bool(self._oauth_client_id(provider, settings or self.settings_store.email_settings()))

    def begin_oauth(self, provider: str, redirect_uri: str) -> dict[str, Any]:
        """Purpose: create a provider authorization URL for the browser."""

        provider = self._clean_provider(provider)
        settings = self.settings_store.email_settings()
        client_id = self._oauth_client_id(provider, settings)
        if not client_id:
            raise DiagnosticEmailError(f"{OAUTH_PROVIDER_CONFIGS[provider].display_name} sign-in is not configured in this build")
        pending = self.oauth_clients[provider].create_authorization_request(client_id=client_id, redirect_uri=redirect_uri)
        self._prune_pending_oauth()
        self.pending_oauth[pending.state] = pending
        return {
            "provider": provider,
            "authorization_url": pending.authorization_url,
            "expires_in": OAUTH_PENDING_TTL_SECONDS,
        }

    def finish_oauth(
        self,
        provider: str,
        *,
        state: str,
        code: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Purpose: exchange the browser callback code and save the token."""

        provider = self._clean_provider(provider)
        if error:
            raise DiagnosticEmailError(f"{OAUTH_PROVIDER_CONFIGS[provider].display_name} authorization failed: {error}")
        if not code:
            raise DiagnosticEmailError("OAuth callback did not include an authorization code")
        pending = self.pending_oauth.pop(state, None)
        if not pending or pending.provider != provider:
            raise DiagnosticEmailError("OAuth callback state was not recognized")
        if time.time() - pending.created_at > OAUTH_PENDING_TTL_SECONDS:
            raise DiagnosticEmailError("OAuth callback expired; start the connection again")
        settings = self.settings_store.email_settings()
        client_id = self._oauth_client_id(provider, settings)
        token = self.oauth_clients[provider].exchange_code(client_id=client_id, code=code, pending=pending)
        self._write_oauth_token(provider, token)
        settings.delivery_method = self._delivery_method_for_provider(provider)
        settings.oauth_email = token.email or settings.oauth_email
        self.settings_store.save_email_settings(settings)
        return {"connected": True, "provider": provider, "email": token.email, **self.status()}

    def send_test_email(self) -> dict[str, Any]:
        """Purpose: validate selected diagnostic email delivery with a small message."""

        settings = self._configured_settings()
        subject = self._subject("test", None)
        body = json.dumps(
            {
                "kind": "diagnostic_email_test",
                "support_install_id": self.settings_store.support_install_id(),
                "app_version": self._app_version(),
            },
            indent=2,
        )
        self._send_message(settings=settings, subject=subject, body=body)
        return {"sent": True, **self.status()}

    def send_run_report(
        self,
        run_id: int | None,
        *,
        include_state_bundle: bool = False,
        state_bundle_path: Path | None = None,
    ) -> dict[str, Any]:
        """Purpose: send a manual diagnostic report and optional bundle."""

        settings = self._configured_settings()
        report = self.build_report(run_id)
        attachments = [state_bundle_path] if include_state_bundle and state_bundle_path else []
        self._send_message(
            settings=settings,
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
            settings = self._configured_settings()
            report = self.build_report(run_id)
            self._send_message(
                settings=settings,
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

    def _configured_settings(self) -> DiagnosticEmailSettings:
        """Purpose: load validated settings for the selected delivery method."""

        settings = self.settings_store.email_settings()
        provider = self._provider_for_settings(settings)
        if not settings.is_configured(
            credential_saved=self.email_credential_saved(),
            oauth_token_saved=self.oauth_token_saved(provider) if provider else False,
            oauth_client_configured=self.oauth_client_configured(provider, settings) if provider else False,
        ):
            raise DiagnosticEmailError("Diagnostic email settings are incomplete")
        return settings

    def _send_message(
        self,
        *,
        settings: DiagnosticEmailSettings,
        subject: str,
        body: str,
        attachments: list[Path] | None = None,
    ) -> None:
        """Purpose: route one diagnostic message through SMTP or API delivery."""

        provider = self._provider_for_settings(settings)
        if not provider:
            password = self._read_smtp_password()
            self.sender.send(settings=settings, password=password, subject=subject, body=body, attachments=attachments)
            return
        token = self._fresh_oauth_token(provider, settings)
        self.oauth_senders[provider].send(
            settings=settings,
            access_token=token.access_token,
            subject=subject,
            body=body,
            attachments=attachments,
        )

    def _read_smtp_password(self) -> str:
        """Purpose: read the SMTP secret only when SMTP delivery is selected."""

        try:
            return self.credential_store.read_secret() or ""
        except Exception as exc:
            raise DiagnosticEmailError(f"Could not read diagnostic email credential: {exc}") from exc

    def _save_password(self, password: str) -> str | None:
        """Purpose: store SMTP credential without interrupting settings save."""

        if not self.credential_store.is_available():
            return "Windows Credential Manager is not available on this platform"
        try:
            self.credential_store.write_secret(password)
        except Exception as exc:
            return str(exc)
        return None

    def _provider_for_settings(self, settings: DiagnosticEmailSettings) -> str | None:
        """Purpose: map the selected delivery method to an OAuth provider."""

        return OAUTH_DELIVERY_METHODS.get(settings.delivery_method)

    def _clean_provider(self, provider: str) -> str:
        """Purpose: reject unsupported OAuth providers before network calls."""

        cleaned = provider.strip().lower()
        if cleaned not in OAUTH_PROVIDER_CONFIGS:
            raise DiagnosticEmailError(f"Unsupported diagnostic email OAuth provider: {provider}")
        return cleaned

    def _delivery_method_for_provider(self, provider: str) -> str:
        """Purpose: store the matching delivery method after OAuth succeeds."""

        if provider == GOOGLE_PROVIDER:
            return GMAIL_DELIVERY_METHOD
        if provider == MICROSOFT_PROVIDER:
            return MICROSOFT_DELIVERY_METHOD
        raise DiagnosticEmailError(f"Unsupported diagnostic email OAuth provider: {provider}")

    def _oauth_client_id(self, provider: str, settings: DiagnosticEmailSettings) -> str:
        """Purpose: use developer-owned OAuth app IDs from build or env config."""

        if provider == GOOGLE_PROVIDER:
            return os.getenv("PCW_GOOGLE_OAUTH_CLIENT_ID", "").strip() or oauth_defaults.GOOGLE_CLIENT_ID.strip()
        if provider == MICROSOFT_PROVIDER:
            return os.getenv("PCW_MICROSOFT_OAUTH_CLIENT_ID", "").strip() or oauth_defaults.MICROSOFT_CLIENT_ID.strip()
        return ""

    def _read_oauth_token(self, provider: str) -> OAuthToken:
        """Purpose: read one saved provider token from Credential Manager."""

        try:
            secret = self.oauth_stores[provider].read_secret() or ""
        except Exception as exc:
            raise DiagnosticEmailError(f"Could not read {provider} OAuth token: {exc}") from exc
        if not secret:
            raise DiagnosticEmailError(f"{OAUTH_PROVIDER_CONFIGS[provider].display_name} is not connected")
        return OAuthToken.from_secret(secret, provider=provider)

    def _write_oauth_token(self, provider: str, token: OAuthToken) -> None:
        """Purpose: persist a provider token without writing it to settings JSON."""

        store = self.oauth_stores[provider]
        if not store.is_available():
            raise DiagnosticEmailError("Windows Credential Manager is not available on this platform")
        store.write_secret(token.to_secret())

    def _fresh_oauth_token(self, provider: str, settings: DiagnosticEmailSettings) -> OAuthToken:
        """Purpose: refresh expired API access tokens before sending mail."""

        token = self._read_oauth_token(provider)
        if token.access_token_is_fresh():
            return token
        client_id = self._oauth_client_id(provider, settings)
        if not client_id:
            raise DiagnosticEmailError(f"{OAUTH_PROVIDER_CONFIGS[provider].display_name} sign-in is not configured in this build")
        refreshed = self.oauth_clients[provider].refresh_access_token(client_id=client_id, token=token)
        if not refreshed.email:
            refreshed.email = token.email
        self._write_oauth_token(provider, refreshed)
        return refreshed

    def _prune_pending_oauth(self) -> None:
        """Purpose: discard abandoned browser auth requests."""

        now = time.time()
        self.pending_oauth = {
            state: pending
            for state, pending in self.pending_oauth.items()
            if now - pending.created_at <= OAUTH_PENDING_TTL_SECONDS
        }

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
