from __future__ import annotations

import datetime as dt
from pathlib import Path

from plan_commission_workbench import statuses
from plan_commission_workbench.api import PlanCommissionWorkbench
from plan_commission_workbench.export import ExportService
from plan_commission_workbench.models import (
    AgendaClassification,
    AgendaSegment,
    ApplicationExtraction,
    ContactFields,
    FieldEvidence,
)
from plan_commission_workbench.storage import ReviewStore
from plan_commission_workbench.runtime import WorkbenchRuntime


def test_store_dedupe_review_and_export(tmp_path) -> None:
    store = ReviewStore(tmp_path / "workbench.db")
    store.initialize()
    run_id = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 2), None)
    source_id = store.upsert_source_item(
        run_id=run_id,
        source_kind="agenda",
        event_id="27999",
        file_id=None,
        attachment_id=None,
        source_url="https://example.test/agenda.pdf",
        content_hash="agenda-hash",
        processing_status=statuses.AGENDA_HIT,
    )
    agenda_id = store.upsert_agenda_item(
        run_id,
        source_id,
        AgendaSegment("27999", "96005", "88001", dt.date(2026, 6, 1), "Construct apartments"),
        AgendaClassification("96005", statuses.AGENDA_HIT, 0.91, "Housing", "Construct apartments"),
    )

    assert store.agenda_complete("27999", source_url="https://example.test/agenda.pdf")

    app_source = store.upsert_source_item(
        run_id=run_id,
        source_kind="application",
        event_id="27999",
        file_id="88001",
        attachment_id="171817",
        source_url="https://example.test/application.pdf",
        content_hash="app-hash",
        processing_status=statuses.APPLICATION_EXTRACTED,
    )
    extraction_id = store.upsert_application_extraction(
        run_id,
        app_source,
        ApplicationExtraction(
            agenda_item_id=agenda_id,
            source_url="https://example.test/application.pdf",
            attachment_id="171817",
            applicant=ContactFields(name="Jane Applicant"),
            project_contact=ContactFields(email="pat@example.com"),
            owner=ContactFields(),
            section5_description="Construct 48 dwelling units.",
            unit_count=48,
            status=statuses.APPLICATION_EXTRACTED,
            evidence=(FieldEvidence("unit_count", 48, "48 dwelling units", 0.9),),
        ),
    )

    assert store.application_complete(agenda_id, "https://example.test/application.pdf", "171817")

    store.review_application(
        extraction_id,
        statuses.ACCEPTED,
        {"applicant_name": "Jane Corrected"},
        "Looks clean",
    )
    export_path = tmp_path / "accepted.csv"
    result = ExportService(store).export(export_path, statuses.ACCEPTED)

    assert result["row_count"] == 1
    body = export_path.read_text(encoding="utf-8")
    assert "Jane Corrected" in body
    assert "Construct 48 dwelling units" in body


def test_label_export_uses_corrected_clean_contacts_and_reports_qc(tmp_path) -> None:
    store = ReviewStore(tmp_path / "workbench.db")
    store.initialize()
    run_id = store.create_run(dt.date(2026, 6, 1), dt.date(2026, 6, 2), None)
    agenda_source = store.upsert_source_item(
        run_id=run_id,
        source_kind="agenda",
        event_id="27999",
        file_id=None,
        attachment_id=None,
        source_url="https://example.test/agenda.pdf",
        content_hash="agenda-hash",
        processing_status=statuses.AGENDA_HIT,
    )
    agenda_id = store.upsert_agenda_item(
        run_id,
        agenda_source,
        AgendaSegment("27999", "96005", "88001", dt.date(2026, 6, 1), "Construct apartments"),
        AgendaClassification("96005", statuses.AGENDA_HIT, 0.91, "Housing", "Construct apartments"),
    )
    app_source = store.upsert_source_item(
        run_id=run_id,
        source_kind="application",
        event_id="27999",
        file_id="88001",
        attachment_id="171817",
        source_url="https://example.test/application.pdf",
        content_hash="app-hash",
        processing_status=statuses.APPLICATION_EXTRACTED,
    )
    extraction_id = store.upsert_application_extraction(
        run_id,
        app_source,
        ApplicationExtraction(
            agenda_item_id=agenda_id,
            source_url="https://example.test/application.pdf",
            attachment_id="171817",
            applicant=ContactFields(name="Applicant name Jane Raw"),
            project_contact=ContactFields(name="Pat Contact", email="pat@example.com"),
            owner=ContactFields(),
            section5_description="Construct 48 dwelling units.",
            unit_count=48,
            status=statuses.APPLICATION_EXTRACTED,
        ),
    )
    store.review_application(
        extraction_id,
        statuses.ACCEPTED,
        {
            "applicant_name": "Jane Corrected",
            "applicant_company": "Clean Housing LLC",
            "applicant_mailing_address": "123 Main Street, Madison, Wisconsin",
        },
        None,
    )

    export_path = tmp_path / "labels.docx"
    result = ExportService(store).export(export_path, statuses.ACCEPTED)

    assert result["format"] == "docx"
    assert result["row_count"] == 1
    assert result["qc_skipped_count"] == 1
    assert result["qc_issues"][0]["contact_type"] == "project_contact"
    body = _docx_text(export_path)
    assert "Jane Corrected" in body
    assert "Clean Housing LLC" in body
    assert "123 Main Street, Madison, Wisconsin" in body
    assert "Pat Contact" not in body


def test_data_relative_export_path_uses_runtime_data_dir(tmp_path) -> None:
    runtime = WorkbenchRuntime(project_root=tmp_path / "bundle", data_dir=tmp_path / "user-data")
    workbench = PlanCommissionWorkbench(runtime=runtime)

    path = workbench._export_path(Path("data/exports/madison_review.xlsx"))

    assert path == tmp_path / "user-data" / "exports" / "madison_review.xlsx"


def _docx_text(path) -> str:
    """Purpose: read table text from generated mailing-label DOCX files."""

    from docx import Document

    document = Document(path)
    parts: list[str] = []
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.extend(paragraph.text for paragraph in cell.paragraphs)
    return "\n".join(parts)
