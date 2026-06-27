from __future__ import annotations

import pytest

from plan_commission_workbench.settings import CredentialStoreError, OpenAIKeyManager


class FakeCredentialStore:
    """Purpose: exercise saved-key behavior without touching real secrets."""

    def __init__(
        self,
        secret: str | None = None,
        write_error: Exception | None = None,
        available: bool = True,
    ) -> None:
        self.secret = secret
        self.write_error = write_error
        self.available = available
        self.read_calls = 0
        self.written_secret: str | None = None

    def is_available(self) -> bool:
        """Purpose: report configured fake store availability."""

        return self.available

    def read_secret(self) -> str | None:
        """Purpose: return the configured fake secret."""

        self.read_calls += 1
        return self.secret

    def write_secret(self, secret: str) -> None:
        """Purpose: capture writes or simulate a store failure."""

        if self.write_error:
            raise self.write_error
        self.written_secret = secret


def test_load_saved_key_from_credential_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = FakeCredentialStore("sk-saved")
    manager = OpenAIKeyManager(credential_store=store)

    assert manager.load_saved_key() is True

    assert store.read_calls == 1
    assert manager.api_key_present() is True
    assert manager.credential_status()["credential_loaded"] is True
    assert manager.credential_status()["credential_error"] is None


def test_load_saved_key_keeps_existing_environment_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    store = FakeCredentialStore("sk-saved")
    manager = OpenAIKeyManager(credential_store=store)

    assert manager.load_saved_key() is True

    assert store.read_calls == 0
    assert manager.api_key_present() is True


def test_set_process_key_can_persist_to_credential_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = FakeCredentialStore()
    manager = OpenAIKeyManager(credential_store=store)

    manager.set_process_key("sk-entered", persist=True)

    assert store.written_secret == "sk-entered"
    assert manager.api_key_present() is True
    assert manager.credential_status()["credential_saved"] is True
    assert manager.credential_status()["credential_error"] is None


def test_set_process_key_without_persist_does_not_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = FakeCredentialStore()
    manager = OpenAIKeyManager(credential_store=store)

    manager.set_process_key("sk-session")

    assert store.written_secret is None
    assert manager.api_key_present() is True


def test_unavailable_credential_store_is_clean_session_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = FakeCredentialStore(available=False)
    manager = OpenAIKeyManager(credential_store=store)

    manager.set_process_key("sk-session", persist=True)

    assert store.written_secret is None
    assert manager.api_key_present() is True
    assert manager.credential_status()["credential_saved"] is False
    assert manager.credential_status()["credential_error"] is None


def test_persist_failure_keeps_key_active_for_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = FakeCredentialStore(write_error=CredentialStoreError("store locked"))
    manager = OpenAIKeyManager(credential_store=store)

    manager.set_process_key("sk-session", persist=True)

    assert manager.api_key_present() is True
    assert manager.credential_status()["credential_saved"] is False
    assert manager.credential_status()["credential_error"] == "store locked"


def test_invalid_saved_key_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manager = OpenAIKeyManager(credential_store=FakeCredentialStore("not-a-key"))

    assert manager.load_saved_key() is False

    assert manager.api_key_present() is False
    assert "Saved OpenAI API key is invalid" in str(manager.credential_status()["credential_error"])
