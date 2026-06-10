"""Run watchdog for stale or orphaned background scrape work."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from typing import Any

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

        marked = self.store.mark_stale_running_runs(self.stale_after_seconds)
        for row in marked:
            self._stop_live_worker(row)
        return marked

    def _stop_live_worker(self, row: dict[str, Any]) -> None:
        """Purpose: stop stale child workers after the DB is marked failed."""

        pid = row.get("worker_pid")
        if row.get("pid_alive") is not True or row.get("worker_spawned") is not True or not isinstance(pid, int):
            return
        detail = self._kill_process_tree(pid)
        self.store.log_event(row["run_id"], "watchdog_worker_kill", "watchdog", None, detail)

    def _kill_process_tree(self, pid: int) -> str:
        """Purpose: terminate a stale worker and any Docling descendants."""

        if os.name == "nt":
            return self._kill_windows_process_tree(pid)
        try:
            os.killpg(pid, signal.SIGKILL)
            return f"sent SIGKILL to worker process group {pid}"
        except Exception as exc:
            return f"process-group kill failed for worker {pid}: {exc}; {self._kill_direct_process(pid)}"

    def _kill_windows_process_tree(self, pid: int) -> str:
        """Purpose: use taskkill for Windows child process trees."""

        try:
            completed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
        except Exception as exc:
            return f"taskkill failed for worker process tree {pid}: {exc}; {self._kill_direct_process(pid)}"
        detail = (completed.stderr or completed.stdout or "").strip()
        if completed.returncode == 0:
            return f"terminated worker process tree {pid}"
        return f"taskkill exited {completed.returncode} for worker process tree {pid}: {detail[-300:]}; {self._kill_direct_process(pid)}"

    def _kill_direct_process(self, pid: int) -> str:
        """Purpose: provide a last-resort direct worker kill."""

        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            return "direct worker kill requested"
        except Exception as exc:
            return f"direct worker kill failed: {exc}"

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
