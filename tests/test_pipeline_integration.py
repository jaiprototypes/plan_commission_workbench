from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from plan_commission_workbench import statuses
from plan_commission_workbench.api import PlanCommissionWorkbench
from plan_commission_workbench.docling_adapter import DoclingTextExtractor, DoclingTextResult
from plan_commission_workbench.exceptions import DoclingExtractionError
from plan_commission_workbench.llm import LLMJsonClient
from plan_commission_workbench.models import DownloadedFile, EventRecord
from plan_commission_workbench.runtime import WorkbenchRuntime
from plan_commission_workbench.storage import ReviewStore


AGENDA_URL = "https://example.test/agenda.pdf"
SECOND_AGENDA_URL = "https://example.test/agenda-second.pdf"
APP_URL = "https://webapi.legistar.com/v1/madison/Matters/96005/Attachments/171817/File"


class FakeLegistar:
    def __init__(self, include_second_event: bool = False) -> None:
        self.downloads = 0
        self.events = [EventRecord("27999", dt.date(2026, 6, 1), AGENDA_URL)]
        if include_second_event:
            self.events.append(EventRecord("28000", dt.date(2026, 6, 2), SECOND_AGENDA_URL))

    def list_plan_commission_events(self, date_from, date_to, progress_callback=None):
        if progress_callback:
            progress_callback(f"Fake Legistar event lookup from {date_from} to {date_to}")
        return [event for event in self.events if date_from <= event.meeting_date <= date_to]

    def fetch_event_items(self, event_id, progress_callback=None):
        if progress_callback:
            progress_callback(f"Fake Legistar item lookup for event {event_id}")
        if str(event_id) == "28000":
            return [
                {
                    "EventItemMatterId": "97005",
                    "EventItemMatterFile": "99001",
                    "EventItemMatterName": "Conditional Use for a six-story office building",
                    "EventItemMatterAttachments": [
                        {"MatterAttachmentId": "271817", "MatterAttachmentName": "Land Use Application.pdf"}
                    ],
                },
                {
                    "EventItemMatterId": "97006",
                    "EventItemMatterFile": "99002",
                    "EventItemMatterName": "Planning staff report",
                    "EventItemMatterAttachments": [],
                },
            ]
        return [
            {
                "EventItemMatterId": "96005",
                "EventItemMatterFile": "88001",
                "EventItemMatterName": "Conditional Use for a 100-unit apartment building",
                "EventItemMatterAttachments": [
                    {"MatterAttachmentId": "171817", "MatterAttachmentName": "Land Use Application.pdf"}
                ],
            },
            {
                "EventItemMatterId": "96006",
                "EventItemMatterFile": "88002",
                "EventItemMatterName": "Planning staff report",
                "EventItemMatterAttachments": [],
            },
        ]

    def find_application_attachment(self, agenda_item, event_items):
        from plan_commission_workbench.legistar import LegistarClient

        return LegistarClient("madison").find_application_attachment(agenda_item, event_items)

    def download_file(self, url: str, destination: Path) -> DownloadedFile:
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = url.encode("utf-8")
        destination.write_bytes(payload)
        return DownloadedFile(destination, hashlib.sha256(payload).hexdigest())


class FakeDocling(DoclingTextExtractor):
    def __init__(self) -> None:
        self.calls = 0

    def extract_pdf_text(self, pdf_path: Path, output_dir: Path) -> str:
        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        if pdf_path.name == "agenda_28000.pdf":
            return """
            1. 99001 Conditional Use for a six-story office building
            2. 99002 Planning staff report
            """
        if pdf_path.name.startswith("agenda"):
            return """
            1. 88001 Conditional Use for a 100-unit apartment building
            2. 88002 Planning staff report
            """
        return """
            Section 3. Applicant and Project Contact
            Applicant name Jane Applicant
            Applicant company Applicant LLC
            Project contact person Pat Contact
            Project contact email pat@example.com
            Section 4. Other
            Section 5. Project Information
            Project description Construct 100 dwelling units.
            Unit count 100
            Section 6. Signatures
            """

    def extract_pdf_text_result(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool = False,
        use_vlm: bool = False,
        progress_callback=None,
    ) -> DoclingTextResult:
        text = self.extract_pdf_text(pdf_path, output_dir)
        mode = "vlm" if use_vlm else "full_page_ocr" if force_full_page_ocr else "default"
        return DoclingTextResult(text=text, mode=mode, output_path=output_dir / f"{pdf_path.name}.{mode}.docling.txt")


class FailingDocling(DoclingTextExtractor):
    def extract_pdf_text(self, _pdf_path: Path, _output_dir: Path) -> str:
        raise DoclingExtractionError("docling exploded")

    def extract_pdf_text_result(self, _pdf_path: Path, _output_dir: Path, **_kwargs) -> DoclingTextResult:
        raise DoclingExtractionError("docling exploded")


class RetryApplicationDocling(DoclingTextExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.modes: list[str] = []

    def extract_pdf_text_result(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool = False,
        use_vlm: bool = False,
        progress_callback=None,
    ) -> DoclingTextResult:
        self.modes.append("vlm" if use_vlm else "full_page_ocr" if force_full_page_ocr else "default")
        output_dir.mkdir(parents=True, exist_ok=True)
        if pdf_path.name.startswith("agenda"):
            text = """
            1. 88001 Conditional Use for a 100-unit apartment building
            2. 88002 Planning staff report
            """
            return DoclingTextResult(text=text, mode="default", output_path=output_dir / "agenda.default.docling.txt")
        if not force_full_page_ocr:
            return DoclingTextResult(text="Applicant Jane Applicant Project Construct 100 units", mode="default", output_path=output_dir / "app.default.docling.txt")
        text = """
        Section 3. Applicant and Project Contact
        Applicant name Jane Applicant
        Applicant company Applicant LLC
        Project contact person Pat Contact
        Project contact email pat@example.com
        Section 5. Project Information
        Project description Construct 100 dwelling units.
        Unit count 100
        """
        return DoclingTextResult(text=text, mode="full_page_ocr", output_path=output_dir / "app.full_page_ocr.docling.txt")


class VlmApplicationDocling(RetryApplicationDocling):
    def extract_pdf_text_result(
        self,
        pdf_path: Path,
        output_dir: Path,
        *,
        force_full_page_ocr: bool = False,
        use_vlm: bool = False,
        progress_callback=None,
    ) -> DoclingTextResult:
        self.modes.append("vlm" if use_vlm else "full_page_ocr" if force_full_page_ocr else "default")
        output_dir.mkdir(parents=True, exist_ok=True)
        if pdf_path.name.startswith("agenda"):
            return DoclingTextResult(
                text="1. 88001 Conditional Use for a 100-unit apartment building\n2. 88002 Planning staff report",
                mode="default",
                output_path=output_dir / "agenda.default.docling.txt",
            )
        if not use_vlm:
            mode = "full_page_ocr" if force_full_page_ocr else "default"
            return DoclingTextResult(
                text="Applicant Jane Applicant Project Construct 100 units",
                mode=mode,
                output_path=output_dir / f"app.{mode}.docling.txt",
            )
        text = """
        Section 3. Applicant and Project Contact
        Applicant name Jane Applicant
        Applicant company Applicant LLC
        Project contact person Pat Contact
        Project contact email pat@example.com
        Section 5. Project Information
        Project description Construct 100 dwelling units.
        Unit count 100
        """
        return DoclingTextResult(text=text, mode="vlm", output_path=output_dir / "app.vlm.docling.txt")


def responder(_system: str, user: str):
    if '"items"' in user and "section_3_and_5_text" not in user:
        if "97005" in user:
            return {
                "items": [
                    {
                        "city_item_id": "97005",
                        "classification": statuses.AGENDA_HIT,
                        "confidence": 0.94,
                        "reason": "Office building",
                        "evidence_snippet": "six-story office building",
                    },
                    {
                        "city_item_id": "97006",
                        "classification": statuses.NOT_TARGET_PROJECT,
                        "confidence": 0.88,
                        "reason": "Staff report",
                        "evidence_snippet": "Planning staff report",
                    },
                ]
            }
        return {
            "items": [
                {
                    "city_item_id": "96005",
                    "classification": statuses.AGENDA_HIT,
                    "confidence": 0.93,
                    "reason": "New housing development",
                    "evidence_snippet": "100-unit apartment building",
                },
                {
                    "city_item_id": "96006",
                    "classification": statuses.NOT_TARGET_PROJECT,
                    "confidence": 0.88,
                    "reason": "Staff report",
                    "evidence_snippet": "Planning staff report",
                },
            ]
        }
    return {
        "applicant": {"name": "Jane Applicant", "company": "Applicant LLC"},
        "project_contact": {"name": "Pat Contact", "email": "pat@example.com"},
        "owner": {},
        "section5_description": "Construct 100 dwelling units.",
        "unit_count": 100,
        "evidence": [{"field_name": "unit_count", "value": 100, "evidence_snippet": "Unit count 100", "confidence": 0.9}],
    }


def make_workbench(tmp_path, docling, legistar=None):
    runtime = WorkbenchRuntime(project_root=tmp_path, data_dir=tmp_path / "data")
    store = ReviewStore(runtime.db_path)
    return PlanCommissionWorkbench(
        runtime=runtime,
        store=store,
        legistar=legistar or FakeLegistar(),
        docling=docling,
        llm=LLMJsonClient(responder=responder),
    )


def test_full_mocked_run_creates_hit_and_application_then_skips_completed_work(tmp_path) -> None:
    docling = FakeDocling()
    workbench = make_workbench(tmp_path, docling)

    first = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
    second = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))

    assert first["status"] == statuses.COMPLETED
    assert second["status"] == statuses.COMPLETED
    assert second["agenda_total"] == 2
    assert second["agenda_hits"] == 1
    assert second["applications_total"] == 1
    assert second["applications_extracted"] == 1
    assert len(workbench.store.list_agenda_items(statuses.AGENDA_HIT)) == 1
    assert len(workbench.store.list_application_extractions(statuses.APPLICATION_EXTRACTED)) == 1
    assert docling.calls == 2
    assert not (tmp_path / "data" / "tmp" / "run_1").exists()
    event_stages = [event["stage"] for event in workbench.store.list_run_events(1)]
    assert statuses.APPLICATION_DOCLING in event_stages
    assert statuses.APPLICATION_LLM_EXTRACTING in event_stages

    extraction = workbench.store.list_application_extractions(statuses.APPLICATION_EXTRACTED)[0]
    workbench.store.review_application(extraction["id"], statuses.ACCEPTED, {}, None)
    third = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))

    assert third["applications_total"] == 1
    assert third["applications_extracted"] == 1
    assert len(workbench.store.list_application_extractions(statuses.ACCEPTED)) == 1
    assert docling.calls == 2


def test_overlapping_ranges_reuse_existing_rows_and_process_new_dates(tmp_path) -> None:
    docling = FakeDocling()
    legistar = FakeLegistar(include_second_event=True)
    workbench = make_workbench(tmp_path, docling, legistar=legistar)

    first = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 1))
    second = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))

    assert first["agenda_total"] == 2
    assert first["agenda_hits"] == 1
    assert first["applications_total"] == 1
    assert second["status"] == statuses.COMPLETED
    assert second["agenda_total"] == 4
    assert second["agenda_hits"] == 2
    assert second["applications_total"] == 2
    assert second["applications_extracted"] == 2
    assert len(workbench.store.list_agenda_items(statuses.AGENDA_HIT)) == 2
    assert len(workbench.store.list_application_extractions(statuses.APPLICATION_EXTRACTED)) == 2
    assert legistar.downloads == 4
    assert docling.calls == 4


def test_docling_failure_stops_run_and_cleans_temp_files(tmp_path) -> None:
    workbench = make_workbench(tmp_path, FailingDocling())

    result = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))

    assert result["status"] == statuses.FAILED_AGENDA_DOCLING
    assert "docling exploded" in result["last_error"]
    assert not (tmp_path / "data" / "tmp" / "run_1").exists()


def test_application_docling_retries_full_page_ocr_when_sections_are_missing(tmp_path) -> None:
    docling = RetryApplicationDocling()
    workbench = make_workbench(tmp_path, docling)

    result = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
    stages = [event["stage"] for event in workbench.store.list_run_events(1)]

    assert result["status"] == statuses.COMPLETED
    assert docling.modes == ["default", "default", "full_page_ocr"]
    assert "application_docling_retry" in stages
    assert len(workbench.store.list_application_extractions(statuses.APPLICATION_EXTRACTED)) == 1


def test_application_docling_uses_vlm_after_default_and_ocr_miss_sections(tmp_path) -> None:
    docling = VlmApplicationDocling()
    workbench = make_workbench(tmp_path, docling)

    result = workbench.run_madison_range(dt.date(2026, 6, 1), dt.date(2026, 6, 2))
    stages = [event["stage"] for event in workbench.store.list_run_events(1)]

    assert result["status"] == statuses.COMPLETED
    assert docling.modes == ["default", "default", "full_page_ocr", "vlm"]
    assert "application_docling_vlm_retry" in stages
    assert len(workbench.store.list_application_extractions(statuses.APPLICATION_EXTRACTED)) == 1
