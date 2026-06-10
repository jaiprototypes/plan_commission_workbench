"""Runtime path and logging setup for the standalone workbench."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path


class WorkbenchRuntime:
    """Purpose: centralize local paths without relying on global installs."""

    def __init__(self, project_root: Path | None = None, data_dir: Path | None = None) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[1]
        default_data = self.project_root / "data"
        env_data = os.getenv("PCW_DATA_DIR") or os.getenv("PLAN_COMMISSION_WORKBENCH_DATA_ROOT")
        self.data_dir = Path(data_dir or env_data or default_data)
        self.db_path = self.data_dir / "workbench.db"
        self.tmp_dir = self.data_dir / "tmp"
        self.export_dir = self.data_dir / "exports"
        self.cache_dir = self.data_dir / "cache"
        self.diagnostics_dir = self.data_dir / "diagnostics"
        self.run_log_dir = self.data_dir / "run_logs"
        self.server_log_path = self.data_dir / "server.log"
        self.server_error_log_path = self.data_dir / "server.err.log"

    def setup(self) -> None:
        """Purpose: create local folders and configure low-noise logging."""

        for path in (self.data_dir, self.tmp_dir, self.export_dir, self.cache_dir, self.diagnostics_dir, self.run_log_dir):
            path.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=os.getenv("PCW_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")

    def run_tmp_dir(self, run_id: int) -> Path:
        """Purpose: create an isolated temp folder for one processing run."""

        path = self.tmp_dir / f"run_{run_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_run_tmp(self, run_id: int) -> None:
        """Purpose: remove downloaded PDFs and Docling output after a run."""

        shutil.rmtree(self.tmp_dir / f"run_{run_id}", ignore_errors=True)

    def run_worker_log_paths(self, run_id: int) -> tuple[Path, Path]:
        """Purpose: preserve child scrape logs across server restarts."""

        return self.run_log_dir / f"run_{run_id}.log", self.run_log_dir / f"run_{run_id}.err.log"
