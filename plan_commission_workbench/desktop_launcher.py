"""Windows desktop launcher for the local Plan Commission Workbench server."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from plan_commission_workbench.settings import OpenAIKeyManager

HOST = "127.0.0.1"
PORT = 8010
APP_NAME = "Plan Commission Workbench"
READY_ATTEMPTS = 100
READY_DELAY_MS = 250


def default_data_dir() -> Path:
    """Purpose: choose a writable user data folder for desktop builds."""

    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PlanCommissionWorkbench" / "data"


def configure_desktop_environment() -> None:
    """Purpose: keep DB, temp files, and exports outside the bundled executable."""

    os.environ.setdefault("PCW_DATA_DIR", str(default_data_dir()))


def desktop_log_paths() -> tuple[Path, Path]:
    """Purpose: locate desktop logs where non-technical users can retrieve them."""

    data_dir = default_data_dir()
    return data_dir / "server.log", data_dir / "server.err.log"


def configure_desktop_logging() -> tuple[Path, Path]:
    """Purpose: persist packaged-app startup errors that have no console window."""

    log_path, error_path = desktop_log_paths()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(log_path, "a", buffering=1, encoding="utf-8")
    sys.stderr = open(error_path, "a", buffering=1, encoding="utf-8")
    return log_path, error_path


def port_accepts_connections(host: str = HOST, port: int = PORT) -> bool:
    """Purpose: avoid starting a second server on the same local port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def recent_error_summary(error_path: Path, line_count: int = 16) -> str:
    """Purpose: show the actionable tail of the packaged server error log."""

    if not error_path.exists():
        return ""
    lines = error_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_count:])


class DesktopLauncher:
    """Purpose: provide a small Windows shell around the local web workbench."""

    def __init__(self) -> None:
        configure_desktop_environment()
        import tkinter as tk
        from tkinter import messagebox, simpledialog

        self.tk = tk
        self.messagebox = messagebox
        self.simpledialog = simpledialog
        self.server = None
        self.thread: threading.Thread | None = None
        self.startup_error: str | None = None
        self.log_path, self.error_path = configure_desktop_logging()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("460x220")
        self.status = tk.StringVar(value="Starting local workbench...")
        self.url = f"http://{HOST}:{PORT}/"
        self.health_url = f"{self.url}health"
        self.open_button = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.stop)

    def run(self) -> None:
        """Purpose: start the launcher UI event loop."""

        self.root.after(100, self.start)
        self.root.mainloop()

    def _build_ui(self) -> None:
        """Purpose: create a compact desktop control surface."""

        frame = self.tk.Frame(self.root, padx=22, pady=20)
        frame.pack(fill="both", expand=True)
        self.tk.Label(frame, text=APP_NAME, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.tk.Label(frame, textvariable=self.status, wraplength=410, justify="left").pack(anchor="w", pady=(14, 18))
        buttons = self.tk.Frame(frame)
        buttons.pack(anchor="w")
        self.open_button = self.tk.Button(buttons, text="Open Workbench", command=self.open_browser, width=18, state="disabled")
        self.open_button.pack(side="left", padx=(0, 8))
        self.tk.Button(buttons, text="Set API Key", command=self.prompt_for_key, width=14).pack(side="left", padx=(0, 8))
        self.tk.Button(buttons, text="Stop", command=self.stop, width=10).pack(side="left")

    def start(self) -> None:
        """Purpose: prompt for critical config, launch server, and open browser."""

        self.prompt_for_key(silent_cancel=True)
        if self._health_ready():
            self._mark_ready("Workbench is already running.")
            return
        if port_accepts_connections():
            self._fail_startup("Port 8010 is already in use, but it is not the Plan Commission Workbench.")
            return
        self.thread = threading.Thread(target=self._run_server, name="pcw-server", daemon=True)
        self.thread.start()
        self.root.after(READY_DELAY_MS, self._wait_for_server)

    def prompt_for_key(self, silent_cancel: bool = False) -> None:
        """Purpose: collect a credited OpenAI key without storing it on disk."""

        manager = OpenAIKeyManager()
        if manager.api_key_present():
            return
        api_key = self.simpledialog.askstring(
            APP_NAME,
            "Enter a credited OpenAI API key for this session.\nIt is not saved into the app or written to disk.",
            show="*",
            parent=self.root,
        )
        if not api_key:
            if not silent_cancel:
                self.messagebox.showwarning(APP_NAME, "OpenAI API key is still missing. Scrape runs will be blocked.")
            return
        try:
            manager.set_process_key(api_key)
        except ValueError as exc:
            self.messagebox.showerror(APP_NAME, str(exc))

    def _run_server(self) -> None:
        """Purpose: start FastAPI in the background and preserve failures."""

        try:
            import uvicorn

            from plan_commission_workbench.server import app

            config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info", access_log=True)
            self.server = uvicorn.Server(config)
            self.server.run()
        except Exception:
            self.startup_error = traceback.format_exc()
            print(self.startup_error, file=sys.stderr)

    def _wait_for_server(self, attempts: int = 0) -> None:
        """Purpose: open the browser after FastAPI is ready."""

        if self._health_ready():
            self._mark_ready("Workbench is running.")
            self.open_browser()
            return
        if self.startup_error:
            self._fail_startup("The local workbench server failed while starting.")
            return
        if self.thread and not self.thread.is_alive():
            self._fail_startup("The local workbench server stopped before it became ready.")
            return
        if attempts > READY_ATTEMPTS:
            self._fail_startup("The local workbench server did not become ready in time.")
            return
        self.status.set("Starting server...")
        self.root.after(READY_DELAY_MS, lambda: self._wait_for_server(attempts + 1))

    def _health_ready(self) -> bool:
        """Purpose: verify the server endpoint is responding before opening a browser."""

        try:
            with urlopen(self.health_url, timeout=0.35) as response:
                return response.status == 200
        except (OSError, URLError, TimeoutError):
            return False

    def _mark_ready(self, message: str) -> None:
        """Purpose: enable browser access only after the local server is live."""

        self.status.set(f"{message} {self.url}")
        self._set_open_enabled(True)

    def _set_open_enabled(self, enabled: bool) -> None:
        """Purpose: prevent users from opening a dead localhost URL."""

        if self.open_button is not None:
            self.open_button.config(state="normal" if enabled else "disabled")

    def _fail_startup(self, message: str) -> None:
        """Purpose: surface packaged-server failures without requiring an IDE."""

        self._set_open_enabled(False)
        summary = recent_error_summary(self.error_path)
        detail = f"{message}\n\nError log:\n{self.error_path}"
        if summary:
            detail = f"{detail}\n\nRecent error:\n{summary}"
        self.status.set(f"{message} See {self.error_path}")
        self.messagebox.showerror(APP_NAME, detail)

    def open_browser(self) -> None:
        """Purpose: launch the default browser on the local workbench."""

        if not self._health_ready():
            self.status.set("Workbench is still starting. The browser will open when the server is ready.")
            return
        webbrowser.open(self.url)

    def stop(self) -> None:
        """Purpose: shut down the server thread before closing the launcher."""

        if self.server:
            self.status.set("Stopping server...")
            self.server.should_exit = True
            time.sleep(0.2)
        self.root.destroy()


def main() -> None:
    """Purpose: executable entry point for PyInstaller and local smoke tests."""

    DesktopLauncher().run()


if __name__ == "__main__":
    main()
