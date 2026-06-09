"""Run watchdog for stale or orphaned background scrape work."""

from __future__ import annotations

import logging
import os
import threading

from .storage import ReviewStore

LOG = logging.getLogger(__name__)


class RunWatchdog:
    """Purpose: fail running DB rows when processing stops reporting progress."""

    def __init__(
        self,
        store: ReviewStore,
        *,
        interval_seconds: int | None = None,
        stale_after_seconds: int | None = None,
    ) -> None:
        self.store = store
        self.interval_seconds = interval_seconds or self._env_int("PCW_WATCHDOG_INTERVAL_SECONDS", 30)
        self.stale_after_seconds = stale_after_seconds or self._env_int("PCW_RUN_STALE_SECONDS", 900)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Purpose: start one daemon listener for the local server process."""

        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pcw-run-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Purpose: stop the daemon listener during server shutdown."""

        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def audit_once(self) -> list[dict]:
        """Purpose: expose one watchdog pass for tests and startup recovery."""

        return self.store.mark_stale_running_runs(self.stale_after_seconds)

    def _loop(self) -> None:
        """Purpose: audit immediately, then keep checking until shutdown."""

        self._safe_audit()
        while not self._stop.wait(self.interval_seconds):
            self._safe_audit()

    def _safe_audit(self) -> None:
        """Purpose: prevent watchdog errors from taking down the server."""

        try:
            self.audit_once()
        except Exception:
            LOG.exception("Run watchdog audit failed")

    def _env_int(self, name: str, default: int) -> int:
        """Purpose: parse watchdog timing from environment safely."""

        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return max(1, value)
