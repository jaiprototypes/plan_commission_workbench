"""Local runtime settings that should not be persisted to the database."""

from __future__ import annotations

import ctypes
import getpass
import os
import sys
from typing import Protocol


class CredentialStore(Protocol):
    """Purpose: define the local secret-store behavior used by settings."""

    def is_available(self) -> bool:
        """Purpose: report whether the store can be used on this platform."""

        ...

    def read_secret(self) -> str | None:
        """Purpose: read a saved secret without exposing it to logs."""

        ...

    def write_secret(self, secret: str) -> None:
        """Purpose: persist a secret in the current user's local store."""

        ...

    def delete_secret(self) -> bool:
        """Purpose: remove a secret from the local store when present."""

        ...


class CredentialStoreError(RuntimeError):
    """Purpose: report local secret-store failures without including secrets."""


class WindowsCredentialStore:
    """Purpose: store the OpenAI key in Windows Credential Manager."""

    _credential_type = 1
    _persist_local_machine = 2
    _error_not_found = 1168

    def __init__(self, target_name: str) -> None:
        self.target_name = target_name
        self._advapi = None

    def is_available(self) -> bool:
        """Purpose: keep non-Windows builds dependency-free and no-op."""

        return os.name == "nt"

    def read_secret(self) -> str | None:
        """Purpose: load a saved key from the current Windows user profile."""

        if not self.is_available():
            return None
        credential_type = self._credential_struct()
        credential_pointer = ctypes.POINTER(credential_type)()
        cred_read = self._library().CredReadW
        cred_read.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.POINTER(credential_type)),
        ]
        cred_read.restype = ctypes.c_bool
        if not cred_read(self.target_name, self._credential_type, 0, ctypes.byref(credential_pointer)):
            error = ctypes.get_last_error()
            if error == self._error_not_found:
                return None
            raise self._windows_error("read", error)
        try:
            credential = credential_pointer.contents
            if not credential.CredentialBlob or not credential.CredentialBlobSize:
                return None
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return blob.decode("utf-16-le").strip()
        finally:
            self._free_credential(credential_pointer)

    def write_secret(self, secret: str) -> None:
        """Purpose: save a key for the current Windows user only."""

        if not self.is_available():
            raise CredentialStoreError("Windows Credential Manager is not available on this platform")
        blob = secret.encode("utf-16-le")
        blob_buffer = ctypes.create_string_buffer(blob)
        credential_type = self._credential_struct()
        credential = credential_type()
        credential.Type = self._credential_type
        credential.TargetName = self.target_name
        credential.Comment = "Stored by Plan Commission Workbench"
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._persist_local_machine
        credential.UserName = "OpenAI API key"
        cred_write = self._library().CredWriteW
        cred_write.argtypes = [ctypes.POINTER(credential_type), ctypes.c_ulong]
        cred_write.restype = ctypes.c_bool
        if not cred_write(ctypes.byref(credential), 0):
            raise self._windows_error("write", ctypes.get_last_error())

    def delete_secret(self) -> bool:
        """Purpose: remove this target from Windows Credential Manager."""

        if not self.is_available():
            return False
        cred_delete = self._library().CredDeleteW
        cred_delete.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong]
        cred_delete.restype = ctypes.c_bool
        if cred_delete(self.target_name, self._credential_type, 0):
            return True
        error = ctypes.get_last_error()
        if error == self._error_not_found:
            return False
        raise self._windows_error("delete", error)

    def _library(self):
        """Purpose: load Advapi32 lazily so non-Windows tests can import."""

        if self._advapi is None:
            self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        return self._advapi

    def _free_credential(self, credential_pointer) -> None:
        """Purpose: release native memory allocated by CredReadW."""

        cred_free = self._library().CredFree
        cred_free.argtypes = [ctypes.c_void_p]
        cred_free.restype = None
        cred_free(ctypes.cast(credential_pointer, ctypes.c_void_p))

    def _credential_struct(self):
        """Purpose: describe the Windows CREDENTIALW layout for ctypes."""

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong),
            ]

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", ctypes.c_ulong),
                ("Type", ctypes.c_ulong),
                ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p),
                ("LastWritten", FileTime),
                ("CredentialBlobSize", ctypes.c_ulong),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", ctypes.c_ulong),
                ("AttributeCount", ctypes.c_ulong),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p),
            ]

        return Credential

    def _windows_error(self, operation: str, error: int) -> CredentialStoreError:
        """Purpose: convert Win32 failures into safe Python exceptions."""

        message = ctypes.FormatError(error).strip()
        return CredentialStoreError(f"Windows Credential Manager {operation} failed: {message}")


class OpenAIKeyManager:
    """Purpose: manage the required OpenAI API key for local app launches."""

    env_name = "OPENAI_API_KEY"
    credential_target = "PlanCommissionWorkbench/OpenAIAPIKey"

    def __init__(self, credential_store: CredentialStore | None = None) -> None:
        self.credential_store = credential_store or WindowsCredentialStore(self.credential_target)
        self.credential_loaded = False
        self.credential_saved = False
        self.credential_error: str | None = None

    def api_key_present(self) -> bool:
        """Purpose: report whether the process can make OpenAI API calls."""

        return bool(os.getenv(self.env_name, "").strip())

    def load_saved_key(self) -> bool:
        """Purpose: hydrate the process from the current user's saved key."""

        if self.api_key_present():
            return True
        try:
            api_key = self.credential_store.read_secret()
        except Exception as exc:
            self.credential_error = str(exc)
            return False
        if not api_key:
            return False
        self.credential_error = None
        try:
            self.set_process_key(api_key)
        except ValueError as exc:
            self.credential_error = f"Saved OpenAI API key is invalid: {exc}"
            return False
        self.credential_loaded = True
        return True

    def set_process_key(self, api_key: str, *, persist: bool = False) -> None:
        """Purpose: set a credited key and optionally save it locally."""

        cleaned = api_key.strip()
        if not cleaned:
            raise ValueError("OpenAI API key is required")
        if not cleaned.startswith("sk-"):
            raise ValueError("OpenAI API key should start with sk-")
        os.environ[self.env_name] = cleaned
        if persist:
            self._persist_key(cleaned)

    def credential_status(self) -> dict[str, object]:
        """Purpose: expose saved-key state without exposing the key itself."""

        return {
            "credential_store_available": self.credential_store.is_available(),
            "credential_loaded": self.credential_loaded,
            "credential_saved": self.credential_saved,
            "credential_error": self.credential_error,
        }

    def clear_saved_key(self, *, clear_process: bool = True) -> dict[str, object]:
        """Purpose: remove the local OpenAI key without touching app data."""

        if clear_process:
            os.environ.pop(self.env_name, None)
        try:
            deleted = self.credential_store.delete_secret()
        except Exception as exc:
            self.credential_error = str(exc)
            return {"credential_deleted": False, **self.credential_status()}
        self.credential_loaded = False
        self.credential_saved = False
        self.credential_error = None
        return {"credential_deleted": deleted, **self.credential_status()}

    def prompt_if_missing(self, *, required: bool) -> bool:
        """Purpose: ask terminal users for a credited key without echoing it."""

        if self.load_saved_key():
            return True
        if self.api_key_present():
            return True
        if not sys.stdin.isatty():
            if required:
                raise RuntimeError("OPENAI_API_KEY is required for Madison runs")
            return False
        prompt = "Enter credited OpenAI API key (saved to Windows Credential Manager when available): "
        api_key = getpass.getpass(prompt).strip()
        if not api_key:
            if required:
                raise RuntimeError("OPENAI_API_KEY is required for Madison runs")
            return False
        self.set_process_key(api_key, persist=True)
        return True

    def _persist_key(self, api_key: str) -> None:
        """Purpose: keep local persistence best-effort and non-blocking."""

        if not self.credential_store.is_available():
            self.credential_saved = False
            self.credential_error = None
            return
        try:
            self.credential_store.write_secret(api_key)
        except Exception as exc:
            self.credential_saved = False
            self.credential_error = str(exc)
            return
        self.credential_saved = True
        self.credential_error = None
