"""Windows desktop launcher for the local Plan Commission Workbench server."""

from __future__ import annotations

import os
import socket
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .settings import OpenAIKeyManager

HOST = "127.0.0.1"
PORT = 8010
URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"{URL}health"
APP_NAME = "Plan Commission Workbench"


def default_data_dir() -> Path:
    """Purpose: choose a writable user data folder for desktop builds."""

    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PlanCommissionWorkbench" / "data"


def configure_desktop_environment() -> None:
    """Purpose: keep DB, temp files, and exports outside the bundled executable."""

    os.environ.setdefault("PCW_DATA_DIR", str(default_data_dir()))


def port_accepts_connections(host: str = HOST, port: int = PORT) -> bool:
    """Purpose: avoid starting a second server on the same local port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


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
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("460x220")
        self.status = tk.StringVar(value="Starting local workbench...")
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
        self.tk.Button(buttons, text="Open Workbench", command=self.open_browser, width=18).pack(side="left", padx=(0, 8))
        self.tk.Button(buttons, text="Set API Key", command=self.prompt_for_key, width=14).pack(side="left", padx=(0, 8))
        self.tk.Button(buttons, text="Stop", command=self.stop, width=10).pack(side="left")

    def start(self) -> None:
        """Purpose: prompt for critical config, launch server, and open browser."""

        self.prompt_for_key(silent_cancel=True)
        if port_accepts_connections():
            self.status.set(f"Workbench is already running at {URL}")
            self.open_browser()
            return
        try:
            import uvicorn
        except Exception as exc:
            self.messagebox.showerror(APP_NAME, f"uvicorn is missing: {exc}")
            self.status.set("Unable to start because uvicorn is missing.")
            return
        self.server = uvicorn.Server(uvicorn.Config("plan_commission_workbench.server:app", host=HOST, port=PORT, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, name="pcw-server", daemon=True)
        self.thread.start()
        self.root.after(250, self._wait_for_server)

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

    def _wait_for_server(self, attempts: int = 0) -> None:
        """Purpose: open the browser after FastAPI is ready."""

        if self._health_ready():
            self.status.set(f"Workbench is running at {URL}")
            self.open_browser()
            return
        if attempts > 80:
            self.status.set("Server did not become ready. Check the build logs or restart the launcher.")
            return
        self.status.set("Starting server...")
        self.root.after(250, lambda: self._wait_for_server(attempts + 1))

    def _health_ready(self) -> bool:
        """Purpose: verify the server endpoint is responding before opening a browser."""

        try:
            with urlopen(HEALTH_URL, timeout=0.35) as response:
                return response.status == 200
        except (OSError, URLError, TimeoutError):
            return False

    def open_browser(self) -> None:
        """Purpose: launch the default browser on the local workbench."""

        webbrowser.open(URL)

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
