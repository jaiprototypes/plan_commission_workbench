"""Behavioral checks for local extraction, persistence, and honest coverage."""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_commission_workbench.cities.milwaukee.cpc_mvp import MilwaukeeCpcMvpService
from plan_commission_workbench.cities.milwaukee.local_reader import LocalDocumentProcessor, MilwaukeeLocalReader
from plan_commission_workbench.local_document_store import LocalDocumentStore
from plan_commission_workbench.models import EventRecord


class FakeLegistar:
    """Purpose: make network requests observable in local-reader tests."""

    def __init__(self):
        self.downloads = 0

    def download_file(self, url, path):
        self.downloads += 1
        path.write_bytes(b"fixture PDF")
        return SimpleNamespace(path=path, content_hash="source-hash")

    def list_body_events(self, body, start, end):
        return [EventRecord("1", start, "https://example.test/agenda.pdf")]

    def fetch_event_items(self, event_id):
        return [{"EventItemMatterId": 10, "EventItemMatterFile": "260001",
                 "EventItemTitle": "New multifamily development",
                 "EventItemMatterAttachments": [
                     {"MatterAttachmentName": "Building Plan Sheets", "MatterAttachmentId": 12,
                      "MatterAttachmentHyperlink": "https://example.test/plans.pdf"},
                     {"MatterAttachmentName": "Staff Report", "MatterAttachmentId": 13,
                      "MatterAttachmentHyperlink": "https://example.test/staff.pdf"}]}]


class FakeReader:
    """Purpose: represent a deliberately incomplete source-page extraction."""

    def read(self, path, *, use_ocr=True):
        return {"page_count": 1, "text_chars": 50, "coverage_complete": False,
                "warnings": ["Image needs OCR"], "pages": [
                    {"page": 1, "coverage": "needs_ocr", "issues": ["OCR disabled"],
                     "blocks": [{"text": "OWNER\nExample LLC", "bbox": [0, 0, 10, 10], "method": "text_layer"}]}]}


class FakeSelector:
    """Purpose: isolate transport and source association from layout rules."""

    def extract(self, document):
        return {"contacts": [{"company": "Example LLC", "role": "owner", "pdf_page": 1}],
                "unresolved_blocks": [], "warnings": []}


def test_cache_avoids_download_and_reader_and_retains_source(tmp_path):
    legistar = FakeLegistar()
    store = LocalDocumentStore(tmp_path / "evidence.sqlite3")
    processor = LocalDocumentProcessor(legistar, store, FakeReader(), FakeSelector(), tmp_path)
    document = {"name": "Plan Sheets", "source_url": "https://example.test/plans.pdf"}
    first = processor.process(document, use_ocr=False)

    class MustNotRead:
        def read(self, *args, **kwargs):
            raise AssertionError("Cached source was extracted again")

    processor.reader = MustNotRead()
    second = processor.process(document, use_ocr=False)
    assert legistar.downloads == 1
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert second["contacts"][0]["source_url"] == document["source_url"]
    assert second["contacts"][0]["pdf_page"] == 1
    assert second["coverage"]["coverage_complete"] is False
    assert not list(tmp_path.glob("pcw_local_document_*"))


def test_refresh_and_ocr_mode_do_not_reuse_incompatible_cache(tmp_path):
    legistar = FakeLegistar()
    processor = LocalDocumentProcessor(legistar, LocalDocumentStore(tmp_path / "evidence.sqlite3"), FakeReader(), FakeSelector(), tmp_path)
    document = {"name": "Plans", "source_url": "https://example.test/plans.pdf"}
    processor.process(document, use_ocr=False)
    processor.process(document, use_ocr=True)
    processor.process(document, use_ocr=True, refresh_sources=True)
    assert legistar.downloads == 3


def test_cache_read_failure_is_logged_without_downloading(caplog):
    """Corrupt evidence storage is a visible failure, not a reason to rescrape."""

    class BrokenStore:
        def get(self, *args):
            raise ValueError("Corrupt cached evidence")

    legistar = FakeLegistar()
    processor = LocalDocumentProcessor(legistar, BrokenStore())
    with pytest.raises(ValueError, match="Corrupt cached evidence"):
        processor.process({"name": "Plans", "source_url": "https://example.test/plans.pdf"})
    assert legistar.downloads == 0
    assert "https://example.test/plans.pdf" in caplog.text and "stopping" in caplog.text


def test_reader_failure_stops_logs_and_removes_pdf(tmp_path, caplog):
    class BrokenReader:
        def read(self, *args, **kwargs):
            raise RuntimeError("OCR failed")

    store = LocalDocumentStore(tmp_path / "evidence.sqlite3")
    processor = LocalDocumentProcessor(FakeLegistar(), store, BrokenReader(), FakeSelector(), tmp_path)
    with pytest.raises(RuntimeError, match="OCR failed"):
        processor.process({"name": "Plans", "source_url": "https://example.test/plans.pdf"})
    assert "stopping" in caplog.text
    assert store.get("https://example.test/plans.pdf", "local-contacts-v1:ocr=True") is None
    assert not list(tmp_path.glob("pcw_local_document_*"))


def test_plans_read_without_llm_and_document_limit_is_explicit(tmp_path):
    class NoLlm:
        def __getattr__(self, name):
            raise AssertionError("Local mode accessed the LLM")

    legistar = FakeLegistar()
    discovery = MilwaukeeCpcMvpService(legistar=legistar, llm=NoLlm(), tmp_root=tmp_path)
    processor = LocalDocumentProcessor(legistar, reader=FakeReader(), selector=FakeSelector(), tmp_root=tmp_path)
    result = MilwaukeeLocalReader(discovery, processor).run(
        date_from=dt.date(2026, 6, 1), date_to=dt.date(2026, 6, 30), documents_per_item=1, use_ocr=False,
    )
    project = result["projects"][0]
    assert project["evidence_documents"][0]["name"] == "Building Plan Sheets"
    assert project["document_selection_complete"] is False
    assert project["target_project"] is None
    assert result["coverage_complete"] is False
    assert result["llm_calls"] == 0
    assert "read 1 of 2" in " ".join(project["extraction"]["review_notes"])


def test_viewer_urls_supported_and_unread_file_types_limit_coverage(tmp_path):
    """Supporting Word files must remain visible even when every PDF was read."""

    discovery = MilwaukeeCpcMvpService(legistar=FakeLegistar(), tmp_root=tmp_path)
    reader = MilwaukeeLocalReader(discovery)
    raw = {"EventItemMatterAttachments": [
        {"MatterAttachmentName": "Exhibit A", "MatterAttachmentHyperlink": "https://example.test/View.ashx?M=F&ID=1"},
        {"MatterAttachmentName": "Project contacts.docx", "MatterAttachmentHyperlink": "https://example.test/Attachments/2/File"},
    ]}
    documents = reader._documents(raw)
    skipped = reader._skipped_attachments(raw)
    assert len(documents) == 1 and documents[0]["name"] == "Exhibit A"
    assert len(skipped) == 1 and skipped[0]["name"] == "Project contacts.docx"

    class CompleteProcessor:
        def process(self, document, **kwargs):
            return document | {"contacts": [], "unresolved_blocks": [], "warnings": [],
                               "coverage": {"coverage_complete": True}}

    reader.processor = CompleteProcessor()
    project = reader._read_item({"matter_id": "1", "city_file": "123", "title": "Project",
                                "evidence_documents": documents, "skipped_attachments": skipped}, 5, False, False)
    assert project["coverage_complete"] is False
    assert project["document_selection_complete"] is True
    assert "Project contacts.docx" in " ".join(project["extraction"]["review_notes"])
