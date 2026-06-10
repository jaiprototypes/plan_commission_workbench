"""Application PDF extraction for agenda-hit rows."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import statuses
from .docling_adapter import DoclingTextExtractor, DoclingTextResult
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
            if not self.store.run_is_running(run_id):
                return
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
        if not self.store.heartbeat_run(
            run_id,
            statuses.APPLICATION_DOWNLOADING,
            "legistar",
            identity,
            f"Downloading application PDF attachment {attachment.attachment_id}",
        ):
            return
        try:
            downloaded = self.legistar.download_file(attachment.source_url, pdf_path)
        except DownloadError as exc:
            self.store.log_event(run_id, statuses.FAILED_APPLICATION_DOWNLOAD, "legistar", identity, str(exc))
            raise WorkbenchStop(statuses.FAILED_APPLICATION_DOWNLOAD, str(exc)) from exc
        if not self.store.run_is_running(run_id):
            downloaded.path.unlink(missing_ok=True)
            return
        self.store.log_event(run_id, "application_downloaded", "legistar", identity, f"Downloaded application PDF: {downloaded.summary()}")
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
            identity = f"agenda_item:{attachment.agenda_item_id}"
            clipped = self._extract_clipped_sections(run_id, identity, pdf_path, docling_dir)
            if not clipped.strip():
                message = f"Sections 3 and 5 were not found in {pdf_path.name} after Docling default and full-page OCR attempts"
                self.store.log_event(
                    run_id,
                    statuses.FAILED_APPLICATION_DOCLING,
                    "application",
                    identity,
                    message,
                )
                raise WorkbenchStop(statuses.FAILED_APPLICATION_DOCLING, message)
            if not self.store.run_is_running(run_id):
                return
            self.store.set_source_status(source_id, statuses.APPLICATION_LLM_EXTRACTING)
            if not self.store.heartbeat_run(
                run_id,
                statuses.APPLICATION_LLM_EXTRACTING,
                "llm",
                identity,
                f"Extracting Section 3/5 fields with OpenAI for attachment {attachment.attachment_id}",
            ):
                return
            extraction = self.llm.extract_application(
                agenda_item_id=attachment.agenda_item_id,
                source_url=attachment.source_url,
                attachment_id=attachment.attachment_id,
                clipped_text=clipped,
            )
            if not self.store.run_is_running(run_id):
                return
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

    def _extract_clipped_sections(self, run_id: int, identity: str, pdf_path: Path, docling_dir: Path) -> str:
        """Purpose: retry bad application Docling output with full-page OCR."""

        clipped = self._try_docling_mode(run_id, identity, pdf_path, docling_dir, force_full_page_ocr=False)
        if clipped.strip():
            return clipped
        if self._full_page_retry_enabled():
            self.store.log_event(
                run_id,
                "application_docling_retry",
                "docling",
                identity,
                f"Retrying {pdf_path.name} with full-page OCR because default Docling did not expose Sections 3 and 5; {self._full_page_ocr_summary()}",
            )
            clipped = self._try_docling_mode(run_id, identity, pdf_path, docling_dir, force_full_page_ocr=True)
            if clipped.strip():
                return clipped
        if self._vlm_retry_enabled():
            self.store.log_event(
                run_id,
                "application_docling_vlm_retry",
                "docling",
                identity,
                f"Retrying {pdf_path.name} with Docling VLM because default/OCR modes did not expose Sections 3 and 5; {self._vlm_summary()}",
            )
            return self._try_docling_mode(run_id, identity, pdf_path, docling_dir, use_vlm=True)
        return ""

    def _try_docling_mode(
        self,
        run_id: int,
        identity: str,
        pdf_path: Path,
        docling_dir: Path,
        *,
        force_full_page_ocr: bool = False,
        use_vlm: bool = False,
    ) -> str:
        """Purpose: convert and clip one Docling mode without blocking later fallbacks."""

        try:
            result = self._extract_docling_mode(
                run_id,
                identity,
                pdf_path,
                docling_dir,
                force_full_page_ocr=force_full_page_ocr,
                use_vlm=use_vlm,
            )
        except DoclingExtractionError as exc:
            stage = "application_docling_vlm_failed" if use_vlm else "application_docling_full_page_failed" if force_full_page_ocr else "application_docling_default_failed"
            self.store.log_event(run_id, stage, "docling", identity, str(exc))
            return ""
        return self._clip_and_log(run_id, identity, result)

    def _extract_docling_mode(
        self,
        run_id: int,
        identity: str,
        pdf_path: Path,
        docling_dir: Path,
        *,
        force_full_page_ocr: bool,
        use_vlm: bool = False,
    ) -> DoclingTextResult:
        """Purpose: run one Docling mode with heartbeat and granular logs."""

        mode = "VLM" if use_vlm else "full-page OCR" if force_full_page_ocr else "default"
        if not self.store.heartbeat_run(
            run_id,
            statuses.APPLICATION_DOCLING,
            "docling",
            identity,
            f"Extracting {pdf_path.name} with Docling {mode}; {self.docling.mode_timeout_summary(force_full_page_ocr, use_vlm)}",
        ):
            raise WorkbenchStop(statuses.FAILED_APPLICATION_DOCLING, "Run stopped before Docling extraction")
        result = self.docling.extract_pdf_text_result(
            pdf_path,
            docling_dir / result_dir_name(force_full_page_ocr, use_vlm),
            force_full_page_ocr=force_full_page_ocr,
            use_vlm=use_vlm,
        )
        self.store.log_event(
            run_id,
            "application_docling_text",
            "docling",
            identity,
            f"Docling {result.mode} extracted {len(result.text)} char(s); sidecar={result.output_path.name}",
        )
        return result

    def _clip_and_log(self, run_id: int, identity: str, result: DoclingTextResult) -> str:
        """Purpose: record whether Docling output contains target sections."""

        clipped = self.clipper.clip_sections_3_and_5(result.text)
        status = "found" if clipped.strip() else "missing"
        self.store.log_event(
            run_id,
            "application_sections_clipped",
            "application",
            identity,
            f"Sections 3/5 {status} from Docling {result.mode}; clipped_chars={len(clipped)}, source_chars={len(result.text)}",
        )
        return clipped

    def _full_page_retry_enabled(self) -> bool:
        """Purpose: allow operators to disable slower OCR retry if needed."""

        return os.getenv("PCW_DOCLING_FULL_PAGE_RETRY", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _vlm_retry_enabled(self) -> bool:
        """Purpose: allow operators to disable the heaviest Docling fallback."""

        return os.getenv("PCW_DOCLING_VLM_FALLBACK", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _full_page_ocr_summary(self) -> str:
        """Purpose: log retry settings without requiring test doubles to inherit state."""

        summary = getattr(self.docling, "full_page_ocr_summary", None)
        if callable(summary):
            try:
                return str(summary())
            except AttributeError:
                pass
        return "backend=unknown, images_scale=unknown"

    def _vlm_summary(self) -> str:
        """Purpose: log VLM settings without requiring test doubles to inherit state."""

        summary = getattr(self.docling, "vlm_summary", None)
        if callable(summary):
            try:
                return str(summary())
            except AttributeError:
                pass
        return "preset=unknown, images_scale=unknown, timeout_seconds=unknown"

    def _event_items(self, event_id: str) -> list[dict]:
        """Purpose: fetch Legistar event items once per run event."""

        if event_id not in self._event_items_cache:
            self._event_items_cache[event_id] = self.legistar.fetch_event_items(event_id)
        return self._event_items_cache[event_id]


def result_dir_name(force_full_page_ocr: bool, use_vlm: bool = False) -> str:
    """Purpose: isolate default and full-page OCR sidecars in temp output."""

    if use_vlm:
        return "vlm"
    return "full_page_ocr" if force_full_page_ocr else "default"
