"""Madison agenda download, Docling extraction, segmentation, and LLM classification."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import statuses
from .docling_adapter import DoclingTextExtractor
from .exceptions import DoclingExtractionError, DownloadError, LLMResponseError, WorkbenchStop
from .legistar import LegistarClient
from .llm import LLMJsonClient
from .models import AgendaClassification, AgendaSegment, EventRecord, RunRequest
from .segmentation import AgendaSegmenter
from .storage import ReviewStore


class AgendaPipeline:
    """Purpose: own the agenda half of a Madison run."""

    def __init__(
        self,
        store: ReviewStore,
        legistar: LegistarClient,
        docling: DoclingTextExtractor,
        llm: LLMJsonClient,
        segmenter: AgendaSegmenter | None = None,
    ) -> None:
        self.store = store
        self.legistar = legistar
        self.docling = docling
        self.llm = llm
        self.segmenter = segmenter or AgendaSegmenter()

    def process_range(self, run_id: int, request: RunRequest, run_tmp: Path) -> None:
        """Purpose: process all Madison events for the requested date range."""

        events = self.legistar.list_plan_commission_events(request.date_from, request.date_to)
        self.store.log_event(run_id, "agenda_events", "legistar", None, f"Fetched {len(events)} Plan Commission event(s)")
        for event in events:
            self._process_event(run_id, event, request, run_tmp)
            self.store.update_counters(run_id)

    def _process_event(self, run_id: int, event: EventRecord, request: RunRequest, run_tmp: Path) -> None:
        """Purpose: process one agenda PDF unless durable data is complete."""

        identity = f"event:{event.event_id}"
        if self.store.agenda_complete(event.event_id, source_url=event.agenda_url):
            self.store.log_event(run_id, "agenda_skip", "agenda", identity, "Agenda already classified by source URL")
            return
        pdf_path = run_tmp / f"agenda_{event.event_id}.pdf"
        try:
            downloaded = self.legistar.download_file(event.agenda_url, pdf_path)
        except DownloadError as exc:
            raise WorkbenchStop(statuses.FAILED_AGENDA_DOCLING, str(exc)) from exc
        try:
            if self.store.agenda_complete(event.event_id, content_hash=downloaded.content_hash):
                self.store.log_event(run_id, "agenda_skip", "agenda", identity, "Agenda already classified by content hash")
                return
            source_id = self.store.upsert_source_item(
                run_id=run_id,
                source_kind="agenda",
                event_id=event.event_id,
                file_id=None,
                attachment_id=None,
                source_url=event.agenda_url,
                content_hash=downloaded.content_hash,
                processing_status=statuses.AGENDA_CLASSIFYING,
            )
            self._extract_segment_classify(run_id, event, request, run_tmp, downloaded.path, source_id)
        finally:
            downloaded.path.unlink(missing_ok=True)

    def _extract_segment_classify(
        self,
        run_id: int,
        event: EventRecord,
        request: RunRequest,
        run_tmp: Path,
        pdf_path: Path,
        source_id: int,
    ) -> None:
        """Purpose: run Docling, local segmentation, and batched classification."""

        identity = f"event:{event.event_id}"
        docling_dir = run_tmp / f"docling_agenda_{event.event_id}"
        try:
            text = self.docling.extract_pdf_text(pdf_path, docling_dir)
            event_items = self.legistar.fetch_event_items(event.event_id)
            segments = self.segmenter.segment(text, event_id=event.event_id, meeting_date=event.meeting_date, event_items=event_items)
            if not segments:
                raise WorkbenchStop(statuses.FAILED_AGENDA_LLM, f"No agenda items were segmented for {identity}")
            classifications = self._classify_chunks(segments, request.request_text)
            self._persist_classifications(run_id, source_id, segments, classifications)
            hit_count = sum(1 for item in classifications if item.classification == statuses.AGENDA_HIT)
            self.store.set_source_status(source_id, statuses.AGENDA_HIT if hit_count else statuses.NOT_TARGET_PROJECT)
            self.store.log_event(run_id, "agenda_classified", "agenda", identity, f"Classified {len(segments)} item(s), {hit_count} hit(s)")
        except DoclingExtractionError as exc:
            raise WorkbenchStop(statuses.FAILED_AGENDA_DOCLING, str(exc)) from exc
        except LLMResponseError as exc:
            raise WorkbenchStop(statuses.FAILED_AGENDA_LLM, str(exc)) from exc
        finally:
            shutil.rmtree(docling_dir, ignore_errors=True)

    def _classify_chunks(
        self,
        segments: list[AgendaSegment],
        request_text: str | None,
        max_chunk_chars: int = 12000,
    ) -> list[AgendaClassification]:
        """Purpose: keep model calls agenda-sized or chunked for longer agendas."""

        results: list[AgendaClassification] = []
        chunk: list[AgendaSegment] = []
        size = 0
        for segment in segments:
            next_size = len(segment.description) + 200
            if chunk and size + next_size > max_chunk_chars:
                results.extend(self.llm.classify_agenda(chunk, request_text))
                chunk = []
                size = 0
            chunk.append(segment)
            size += next_size
        if chunk:
            results.extend(self.llm.classify_agenda(chunk, request_text))
        return results

    def _persist_classifications(
        self,
        run_id: int,
        source_id: int,
        segments: list[AgendaSegment],
        classifications: list[AgendaClassification],
    ) -> None:
        """Purpose: write one classified agenda row per segment."""

        by_city = {item.city_item_id: item for item in classifications}
        for segment in segments:
            self.store.upsert_agenda_item(run_id, source_id, segment, by_city[segment.city_item_id])
