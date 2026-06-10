from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import statuses
from .api import PlanCommissionWorkbench
from .models import RunRequest
from .watchdog import RunWatchdog


PACKAGE_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


class MadisonRunRequest(BaseModel):
    date_from: dt.date
    date_to: dt.date
    request_text: str | None = None


class ReviewRequest(BaseModel):
    status: str
    corrected_fields: dict[str, Any] | None = None
    notes: str | None = None


class ExportRequest(BaseModel):
    output: str = "data/exports/madison_review.xlsx"
    status: str = statuses.ACCEPTED


class OpenAIKeyRequest(BaseModel):
    api_key: str


def create_app(start_watchdog: bool = True) -> FastAPI:
    """Purpose: expose the standalone workbench through API and UI."""

    workbench = PlanCommissionWorkbench()
    watchdog = RunWatchdog(workbench.store) if start_watchdog else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Purpose: run stale-run monitoring for the server lifetime."""

        if watchdog:
            watchdog.start()
        try:
            yield
        finally:
            if watchdog:
                watchdog.stop()

    app = FastAPI(title="Plan Commission Workbench", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def run_screen(request: Request):
        return templates.TemplateResponse(request, "run.html", {"page": "run"})

    @app.get("/agenda", response_class=HTMLResponse)
    def agenda_screen(request: Request):
        return templates.TemplateResponse(request, "agenda.html", {"page": "agenda"})

    @app.get("/applications", response_class=HTMLResponse)
    def applications_screen(request: Request):
        return templates.TemplateResponse(request, "applications.html", {"page": "applications"})

    @app.get("/review", response_class=HTMLResponse)
    def review_screen(request: Request):
        return templates.TemplateResponse(request, "review.html", {"page": "review"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "openai": workbench.openai_status()}

    @app.post("/settings/openai-api-key")
    def openai_api_key(payload: OpenAIKeyRequest) -> dict[str, Any]:
        try:
            return workbench.configure_openai_api_key(payload.api_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/runs/madison")
    def run_madison(payload: MadisonRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            workbench.require_openai_api_key()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        run_id = workbench.create_madison_run(payload.date_from, payload.date_to, payload.request_text)
        request = RunRequest(payload.date_from, payload.date_to, payload.request_text)
        background_tasks.add_task(workbench.execute_madison_run, run_id, request)
        return {"run_id": run_id, "status": statuses.RUNNING}

    @app.get("/runs")
    def runs() -> list[dict[str, Any]]:
        return workbench.store.list_runs()

    @app.get("/runs/{run_id}")
    def run_detail(run_id: int) -> dict[str, Any]:
        row = workbench.store.get_run(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="Run not found")
        return row

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: int) -> list[dict[str, Any]]:
        return workbench.store.list_run_events(run_id)

    @app.post("/runs/{run_id}/retry")
    def retry_run(run_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
        try:
            workbench.require_openai_api_key()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prior = workbench.store.get_run(run_id)
        if not prior:
            raise HTTPException(status_code=404, detail="Run not found")
        date_from = dt.date.fromisoformat(prior["date_from"])
        date_to = dt.date.fromisoformat(prior["date_to"])
        new_run_id = workbench.create_madison_run(date_from, date_to, prior.get("run_request_text"))
        background_tasks.add_task(workbench.execute_madison_run, new_run_id, RunRequest(date_from, date_to, prior.get("run_request_text")))
        return {"run_id": new_run_id, "retry_of": run_id, "status": statuses.RUNNING}

    @app.get("/agenda-items")
    def agenda_items(status: str | None = Query(default=None)) -> list[dict[str, Any]]:
        return workbench.store.list_agenda_items(status)

    @app.get("/application-extractions")
    def application_extractions(status: str | None = Query(default=None)) -> list[dict[str, Any]]:
        rows = workbench.store.list_application_extractions(status)
        for row in rows:
            row["evidence"] = workbench.store.get_field_evidence(int(row["id"]))
        return rows

    @app.patch("/application-extractions/{extraction_id}/review")
    def review_application(extraction_id: int, payload: ReviewRequest) -> dict[str, Any]:
        try:
            return workbench.store.review_application(
                extraction_id,
                payload.status,
                payload.corrected_fields,
                payload.notes,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/exports")
    def exports(payload: ExportRequest) -> dict[str, Any]:
        try:
            return workbench.export_rows(Path(payload.output), payload.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/exports/{export_id}/download")
    def export_download(export_id: int) -> FileResponse:
        row = workbench.store.get_export(export_id)
        if not row:
            raise HTTPException(status_code=404, detail="Export not found")
        path = Path(row["path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Export file is missing")
        media_type = {
            "csv": "text/csv",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(str(row["format"]), "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.post("/diagnostics/state-bundle")
    def create_state_bundle() -> dict[str, Any]:
        try:
            return workbench.create_diagnostic_bundle()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not create diagnostics bundle: {exc}") from exc

    @app.get("/diagnostics/state-bundles/{filename}")
    def download_state_bundle(filename: str) -> FileResponse:
        path = workbench.runtime.diagnostics_dir / filename
        if filename != Path(filename).name or path.suffix.lower() != ".zip":
            raise HTTPException(status_code=400, detail="Invalid diagnostics filename")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Diagnostics bundle not found")
        return FileResponse(path, media_type="application/zip", filename=path.name)

    return app


app = create_app()
