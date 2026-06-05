"""Application PDF extraction for agenda-hit rows."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import statuses
from .docling_adapter import DoclingTextExtractor
from .exceptions import DoclingExtractionError, DownloadError, LLMResponseError, WorkbenchStop
from .legistar import LegistarClient
from .llm import LLMJsonClient
from .models import RunRequest
from .segmentation import SectionClipper
from .storage import ReviewStore


class ApplicationPipeline:
    """Purpose: process only agenda-hit application PDFs."""

    def __init__(
        self,
        store: ReviewStore,
        legistar: LegistarClient,
        docling: DoclingTextExtractor,
        llm: LLMJsonClient,
        clipper: SectionClipper | None = None,
    ) -> None:
        self.store = store
        self.legistar = legistar
        self.docling = docling
        self.llm = llm
        self.clipper = clipper or SectionClipper()
        self._event_items_cache: dict[str, list[dict]] = {}

    def process_hits(self, run_id: int, request: RunRequest, run_tmp: Path) -> None:
        """Purpose: enqueue application work from durable agenda-hit rows."""

        hits = self.store.list_agenda_hits_for_dates(request.date_from, request.date_to)
        self.store.log_event(run_id, "application_queue", "application", None, f"Found {len(hits)} agenda hit(s)")
        for agenda_item in hits:
            self._process_hit(run_id, agenda_item, run_tmp)
            self.store.update_counters(run_id)

    def _process_hit(self, run_id: int, agenda_item: dict, run_tmp: Path) -> None:
        """Purpose: process one agenda hit unless extraction already exists."""

        identity = f"agenda_item:{agenda_item['id']}"
        if self.store.application_complete(int(agenda_item["id"])):
            self.store.log_event(run_id, "application_skip", "application", identity, "Application already extracted or accepted")
            return
        event_items = self._event_items(str(agenda_item["event_id"]))
        attachment = self.legistar.find_application_attachment(agenda_item, event_items)
        if not attachment:
            self.store.log_event(run_id, "application_missing", "application", identity, "No standardized Land Use Application attachment found")
            return
        if self.store.application_complete(attachment.agenda_item_id, attachment.source_url, attachment.attachment_id):
            self.store.log_event(run_id, "application_skip", "application", identity, "Application already extracted by source identity")
            return
        source_id = self.store.upsert_source_item(
            run_id=run_id,
            source_kind="application",
            event_id=str(agenda_item["event_id"]),
            file_id=str(agenda_item.get("file_id") or "") or None,
            attachment_id=attachment.attachment_id,
            source_url=attachment.source_url,
            content_hash=None,
            processing_status=statuses.APPLICATION_QUEUED,
        )
        pdf_path = run_tmp / f"application_{attachment.city_item_id}_{attachment.attachment_id}.pdf"
        self.store.set_source_status(source_id, statuses.APPLICATION_DOWNLOADING)
        try:
            downloaded = self.legistar.download_file(attachment.source_url, pdf_path)
        except DownloadError as exc:
            raise WorkbenchStop(statuses.FAILED_APPLICATION_DOWNLOAD, str(exc)) from exc
        source_id = self.store.upsert_source_item(
            run_id=run_id,
            source_kind="application",
            event_id=str(agenda_item["event_id"]),
            file_id=str(agenda_item.get("file_id") or "") or None,
            attachment_id=attachment.attachment_id,
            source_url=attachment.source_url,
            content_hash=downloaded.content_hash,
            processing_status=statuses.APPLICATION_DOCLING,
        )
        try:
            self._extract_application(run_id, source_id, attachment, downloaded.path, run_tmp)
        finally:
            downloaded.path.unlink(missing_ok=True)

    def _extract_application(self, run_id: int, source_id: int, attachment, pdf_path: Path, run_tmp: Path) -> None:
        """Purpose: run Docling, clip Sections 3/5, and extract fields with LLM."""

        docling_dir = run_tmp / f"docling_application_{attachment.city_item_id}_{attachment.attachment_id}"
        try:
            text = self.docling.extract_pdf_text(pdf_path, docling_dir)
            clipped = self.clipper.clip_sections_3_and_5(text)
            if not clipped.strip():
                message = f"Sections 3 and 5 were not found in {pdf_path.name}"
                self.store.log_event(
                    run_id,
                    statuses.FAILED_APPLICATION_LLM,
                    "application",
                    f"agenda_item:{attachment.agenda_item_id}",
                    message,
                )
                raise WorkbenchStop(statuses.FAILED_APPLICATION_LLM, message)
            self.store.set_source_status(source_id, statuses.APPLICATION_LLM_EXTRACTING)
            extraction = self.llm.extract_application(
                agenda_item_id=attachment.agenda_item_id,
                source_url=attachment.source_url,
                attachment_id=attachment.attachment_id,
                clipped_text=clipped,
            )
            self.store.upsert_application_extraction(run_id, source_id, extraction)
            self.store.set_source_status(source_id, statuses.APPLICATION_EXTRACTED)
            self.store.log_event(
                run_id,
                "application_extracted",
                "application",
                f"agenda_item:{attachment.agenda_item_id}",
                f"Extracted {attachment.name}",
            )
        except DoclingExtractionError as exc:
            self.store.log_event(
                run_id,
                statuses.FAILED_APPLICATION_DOCLING,
                "application",
                f"agenda_item:{attachment.agenda_item_id}",
                f"{attachment.name} attachment {attachment.attachment_id}: {exc}",
            )
            raise WorkbenchStop(statuses.FAILED_APPLICATION_DOCLING, str(exc)) from exc
        except LLMResponseError as exc:
            self.store.log_event(
                run_id,
                statuses.FAILED_APPLICATION_LLM,
                "application",
                f"agenda_item:{attachment.agenda_item_id}",
                f"{attachment.name} attachment {attachment.attachment_id}: {exc}",
            )
            raise WorkbenchStop(statuses.FAILED_APPLICATION_LLM, str(exc)) from exc
        finally:
            shutil.rmtree(docling_dir, ignore_errors=True)

    def _event_items(self, event_id: str) -> list[dict]:
        """Purpose: fetch Legistar event items once per run event."""

        if event_id not in self._event_items_cache:
            self._event_items_cache[event_id] = self.legistar.fetch_event_items(event_id)
        return self._event_items_cache[event_id]
