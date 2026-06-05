"""Public Python facade for the standalone Madison workbench."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from . import statuses
from .agenda_pipeline import AgendaPipeline
from .application_pipeline import ApplicationPipeline
from .docling_adapter import DoclingTextExtractor
from .exceptions import WorkbenchStop
from .export import ExportService
from .legistar import LegistarClient
from .llm import LLMJsonClient
from .models import RunRequest
from .runtime import WorkbenchRuntime
from .settings import OpenAIKeyManager
from .storage import ReviewStore


class PlanCommissionWorkbench:
    """Purpose: coordinate runtime, persistence, pipelines, review, and export."""

    def __init__(
        self,
        *,
        runtime: WorkbenchRuntime | None = None,
        store: ReviewStore | None = None,
        legistar: LegistarClient | None = None,
        docling: DoclingTextExtractor | None = None,
        llm: LLMJsonClient | None = None,
    ) -> None:
        self.runtime = runtime or WorkbenchRuntime()
        self.runtime.setup()
        self.store = store or ReviewStore(self.runtime.db_path)
        self.store.initialize()
        self.legistar = legistar or LegistarClient("madison")
        self.docling = docling or DoclingTextExtractor()
        self.llm = llm or LLMJsonClient()
        self.openai_keys = OpenAIKeyManager()

    def create_madison_run(self, date_from: dt.date, date_to: dt.date, request_text: str | None = None) -> int:
        """Purpose: create a run row before synchronous or background execution."""

        run_id = self.store.create_run(date_from, date_to, request_text)
        self.store.log_event(run_id, "created", "runner", None, f"Created Madison run {date_from} to {date_to}")
        return run_id

    def execute_madison_run(self, run_id: int, request: RunRequest) -> dict[str, Any] | None:
        """Purpose: execute one run and always clean temporary source files."""

        run_tmp = self.runtime.run_tmp_dir(run_id)
        try:
            agenda = AgendaPipeline(self.store, self.legistar, self.docling, self.llm)
            applications = ApplicationPipeline(self.store, self.legistar, self.docling, self.llm)
            if not self._execute_agenda_stage(run_id, request, run_tmp, agenda):
                return self.store.get_run(run_id)
            self._execute_application_stage(run_id, request, run_tmp, applications)
        finally:
            self.runtime.cleanup_run_tmp(run_id)
        return self.store.get_run(run_id)

    def _execute_agenda_stage(
        self,
        run_id: int,
        request: RunRequest,
        run_tmp: Path,
        agenda: AgendaPipeline,
    ) -> bool:
        """Purpose: run agenda work and classify unexpected failures accurately."""

        try:
            agenda.process_range(run_id, request, run_tmp)
            return True
        except WorkbenchStop as exc:
            self.store.fail_run_from_exception(run_id, exc.status, exc)
        except Exception as exc:  # Defensive catch for unexpected agenda failures.
            self.store.fail_run_from_exception(run_id, statuses.FAILED_AGENDA_LLM, exc)
        return False

    def _execute_application_stage(
        self,
        run_id: int,
        request: RunRequest,
        run_tmp: Path,
        applications: ApplicationPipeline,
    ) -> None:
        """Purpose: run application work and classify unexpected failures accurately."""

        try:
            applications.process_hits(run_id, request, run_tmp)
            self.store.update_counters(run_id)
            self.store.finish_run(run_id, statuses.COMPLETED)
            self.store.log_event(run_id, "completed", "runner", None, "Run completed")
        except WorkbenchStop as exc:
            self.store.fail_run_from_exception(run_id, exc.status, exc)
        except Exception as exc:  # Defensive catch for unexpected application failures.
            self.store.fail_run_from_exception(run_id, statuses.FAILED_APPLICATION_LLM, exc)

    def run_madison_range(
        self,
        date_from: dt.date,
        date_to: dt.date,
        request_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Purpose: run a bounded Madison scrape synchronously."""

        request = RunRequest(date_from=date_from, date_to=date_to, request_text=request_text)
        run_id = self.create_madison_run(date_from, date_to, request_text)
        return self.execute_madison_run(run_id, request)

    def retry_run(self, run_id: int) -> dict[str, Any] | None:
        """Purpose: retry a prior run while skip checks reuse completed rows."""

        prior = self.store.get_run(run_id)
        if not prior:
            raise KeyError(f"Run {run_id} not found")
        return self.run_madison_range(
            dt.date.fromisoformat(prior["date_from"]),
            dt.date.fromisoformat(prior["date_to"]),
            prior.get("run_request_text"),
        )

    def export_rows(self, output_path: Path, status: str = statuses.ACCEPTED) -> dict[str, Any]:
        """Purpose: export reviewed rows from SQLite only."""

        path = self._export_path(output_path)
        return ExportService(self.store).export(path, status)

    def _export_path(self, output_path: Path) -> Path:
        """Purpose: keep data-relative exports outside bundled app folders."""

        if output_path.is_absolute():
            return output_path
        if output_path.parts and output_path.parts[0] == "data":
            return self.runtime.data_dir.joinpath(*output_path.parts[1:])
        return self.runtime.project_root / output_path

    def openai_status(self) -> dict[str, Any]:
        """Purpose: expose LLM readiness without making a model call."""

        return self.llm.status()

    def configure_openai_api_key(self, api_key: str) -> dict[str, Any]:
        """Purpose: accept a local-session API key from the startup prompt."""

        self.openai_keys.set_process_key(api_key)
        return self.openai_status()

    def require_openai_api_key(self) -> None:
        """Purpose: stop LLM-backed runs before they fail deeper in the pipeline."""

        if not self.openai_keys.api_key_present():
            raise RuntimeError("OPENAI_API_KEY is required for Madison runs")


def run_madison_range(date_from: dt.date, date_to: dt.date, **kwargs: Any) -> dict[str, Any] | None:
    """Purpose: convenience function for scripts that only need one scrape call."""

    return PlanCommissionWorkbench().run_madison_range(date_from, date_to, **kwargs)
