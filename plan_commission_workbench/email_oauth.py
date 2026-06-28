"""OAuth clients and API senders for diagnostic email delivery."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import requests


GOOGLE_PROVIDER = "gmail"
MICROSOFT_PROVIDER = "microsoft"
GMAIL_DELIVERY_METHOD = "gmail_oauth"
MICROSOFT_DELIVERY_METHOD = "microsoft_oauth"
OAUTH_DELIVERY_METHODS = {
    GMAIL_DELIVERY_METHOD: GOOGLE_PROVIDER,
    MICROSOFT_DELIVERY_METHOD: MICROSOFT_PROVIDER,
}


class OAuthEmailError(RuntimeError):
    """Purpose: report OAuth email failures without exposing tokens."""


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Purpose: define provider-specific OAuth and send-mail endpoints."""

    provider: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class PendingOAuthRequest:
    """Purpose: preserve PKCE state until the browser callback returns."""

    provider: str
    state: str
    code_verifier: str
    redirect_uri: str
    created_at: float
    authorization_url: str


@dataclass
class OAuthToken:
    """Purpose: store OAuth tokens in Windows Credential Manager as JSON."""

    provider: str
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""
    id_token: str = ""
    email: str = ""

    @classmethod
    def from_response(
        cls,
        *,
        provider: str,
        payload: dict[str, Any],
        prior_refresh_token: str = "",
    ) -> "OAuthToken":
        """Purpose: normalize provider token responses and rotated tokens."""

        access_token = str(payload.get("access_token") or "")
        refresh_token = str(payload.get("refresh_token") or prior_refresh_token)
        if not access_token:
            raise OAuthEmailError("OAuth response did not include an access token")
        if not refresh_token:
            raise OAuthEmailError("OAuth response did not include a refresh token")
        expires_in = int(payload.get("expires_in") or 3600)
        id_token = str(payload.get("id_token") or "")
        return cls(
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + max(expires_in - 60, 60),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload.get("scope") or ""),
            id_token=id_token,
            email=email_from_id_token(id_token),
        )

    @classmethod
    def from_secret(cls, secret: str, *, provider: str) -> "OAuthToken":
        """Purpose: parse a saved token without trusting local file shape."""

        try:
            raw = json.loads(secret)
        except json.JSONDecodeError as exc:
            raise OAuthEmailError("Saved OAuth token is unreadable") from exc
        if not isinstance(raw, dict):
            raise OAuthEmailError("Saved OAuth token is invalid")
        return cls(
            provider=provider,
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=float(raw.get("expires_at") or 0),
            token_type=str(raw.get("token_type") or "Bearer"),
            scope=str(raw.get("scope") or ""),
            id_token=str(raw.get("id_token") or ""),
            email=str(raw.get("email") or ""),
        )

    def to_secret(self) -> str:
        """Purpose: serialize only the fields needed for future sends."""

        return json.dumps(
            {
                "provider": self.provider,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "token_type": self.token_type,
                "scope": self.scope,
                "id_token": self.id_token,
                "email": self.email,
            },
            sort_keys=True,
        )

    def access_token_is_fresh(self) -> bool:
        """Purpose: avoid token refresh calls while the access token is valid."""

        return bool(self.access_token and self.expires_at > time.time())


OAUTH_PROVIDER_CONFIGS = {
    GOOGLE_PROVIDER: OAuthProviderConfig(
        provider=GOOGLE_PROVIDER,
        display_name="Gmail",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        scopes=("openid", "email", "https://www.googleapis.com/auth/gmail.send"),
    ),
    MICROSOFT_PROVIDER: OAuthProviderConfig(
        provider=MICROSOFT_PROVIDER,
        display_name="Microsoft",
        authorization_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=("openid", "email", "profile", "offline_access", "https://graph.microsoft.com/Mail.Send"),
    ),
}


class OAuthProviderClient:
    """Purpose: run provider-neutral OAuth authorization-code flow with PKCE."""

    def __init__(self, config: OAuthProviderConfig, *, http=None, timeout_seconds: float = 20.0) -> None:
        self.config = config
        self.http = http or requests.Session()
        self.timeout_seconds = timeout_seconds

    def create_authorization_request(self, *, client_id: str, redirect_uri: str) -> PendingOAuthRequest:
        """Purpose: build the browser URL and retain PKCE verification state."""

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)[:128]
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if self.config.provider == GOOGLE_PROVIDER:
            query["access_type"] = "offline"
            query["prompt"] = "consent"
        url = f"{self.config.authorization_endpoint}?{urlencode(query)}"
        return PendingOAuthRequest(
            provider=self.config.provider,
            state=state,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            created_at=time.time(),
            authorization_url=url,
        )

    def exchange_code(self, *, client_id: str, code: str, pending: PendingOAuthRequest) -> OAuthToken:
        """Purpose: trade the browser authorization code for durable tokens."""

        payload = self._token_payload(
            client_id=client_id,
            grant_type="authorization_code",
            redirect_uri=pending.redirect_uri,
            code=code,
            code_verifier=pending.code_verifier,
        )
        return OAuthToken.from_response(provider=self.config.provider, payload=self._post_token(payload))

    def refresh_access_token(self, *, client_id: str, token: OAuthToken) -> OAuthToken:
        """Purpose: renew expired access tokens without user interaction."""

        payload = self._token_payload(
            client_id=client_id,
            grant_type="refresh_token",
            refresh_token=token.refresh_token,
        )
        return OAuthToken.from_response(
            provider=self.config.provider,
            payload=self._post_token(payload),
            prior_refresh_token=token.refresh_token,
        )

    def _token_payload(self, **values: str) -> dict[str, str]:
        """Purpose: keep token requests explicit and provider-compatible."""

        return {key: value for key, value in values.items() if value}

    def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        """Purpose: send token requests and return provider error detail safely."""

        try:
            response = self.http.post(self.config.token_endpoint, data=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = getattr(exc.response, "text", "") if getattr(exc, "response", None) is not None else ""
            raise OAuthEmailError(f"{self.config.display_name} OAuth token request failed: {detail[:500] or exc}") from exc
        data = response.json()
        if not isinstance(data, dict):
            raise OAuthEmailError(f"{self.config.display_name} OAuth token response was invalid")
        return data


class GmailApiDiagnosticEmailSender:
    """Purpose: send diagnostic mail through the Gmail API."""

    def __init__(self, *, http=None, timeout_seconds: float = 20.0) -> None:
        self.http = http or requests.Session()
        self.timeout_seconds = timeout_seconds

    def send(self, *, settings, access_token: str, subject: str, body: str, attachments: list[Path] | None = None) -> None:
        """Purpose: send one RFC 5322 message through Gmail's send endpoint."""

        message = build_diagnostic_message(settings, subject, body, attachments)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        self._post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            access_token=access_token,
            json_payload={"raw": raw},
        )

    def _post(self, url: str, *, access_token: str, json_payload: dict[str, Any]) -> None:
        """Purpose: isolate HTTP details for tests and safe error reporting."""

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = self.http.post(url, headers=headers, json=json_payload, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = getattr(exc.response, "text", "") if getattr(exc, "response", None) is not None else ""
            raise OAuthEmailError(f"Gmail API send failed: {detail[:500] or exc}") from exc


class MicrosoftGraphDiagnosticEmailSender:
    """Purpose: send diagnostic mail through Microsoft Graph."""

    def __init__(self, *, http=None, timeout_seconds: float = 20.0) -> None:
        self.http = http or requests.Session()
        self.timeout_seconds = timeout_seconds

    def send(self, *, settings, access_token: str, subject: str, body: str, attachments: list[Path] | None = None) -> None:
        """Purpose: send one base64 MIME message through Graph sendMail."""

        message = build_diagnostic_message(settings, subject, body, attachments)
        payload = base64.b64encode(message.as_bytes()).decode("ascii")
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "text/plain"}
        try:
            response = self.http.post(
                "https://graph.microsoft.com/v1.0/me/sendMail",
                headers=headers,
                data=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = getattr(exc.response, "text", "") if getattr(exc, "response", None) is not None else ""
            raise OAuthEmailError(f"Microsoft Graph send failed: {detail[:500] or exc}") from exc


def build_diagnostic_message(settings, subject: str, body: str, attachments: list[Path] | None = None):
    """Purpose: construct one email body for SMTP and API delivery paths."""

    from email.message import EmailMessage

    message = EmailMessage()
    from_address = settings.from_address()
    if from_address:
        message["From"] = from_address
    message["To"] = settings.recipient
    message["Subject"] = subject
    message.set_content(body)
    for path in attachments or []:
        payload = path.read_bytes()
        if path.suffix.lower() == ".zip":
            message.add_attachment(payload, maintype="application", subtype="zip", filename=path.name)
        else:
            message.add_attachment(payload, maintype="application", subtype="octet-stream", filename=path.name)
    return message


def email_from_id_token(id_token: str) -> str:
    """Purpose: display the connected account without validating mail tokens."""

    parts = id_token.split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = _base64url_decode(parts[1])
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    for key in ("email", "preferred_username", "upn"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return ""


def _code_challenge(verifier: str) -> str:
    """Purpose: create an RFC 7636 S256 challenge for native OAuth clients."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    """Purpose: decode JWT segments whose padding is intentionally omitted."""

    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
