"""Small typed records shared by workbench services."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EventRecord:
    """Purpose: carry normalized Madison meeting metadata."""

    event_id: str
    meeting_date: dt.date
    agenda_url: str
    detail_url: str | None = None
    agenda_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgendaSegment:
    """Purpose: represent one local agenda line item before LLM review."""

    event_id: str
    city_item_id: str
    file_id: str | None
    meeting_date: dt.date
    description: str


@dataclass(frozen=True)
class AgendaClassification:
    """Purpose: store one LLM agenda classification result."""

    city_item_id: str
    classification: str
    confidence: float
    reason: str
    evidence_snippet: str


@dataclass(frozen=True)
class AttachmentRecord:
    """Purpose: identify the selected Madison application PDF."""

    agenda_item_id: int
    city_item_id: str
    file_id: str | None
    attachment_id: str
    source_url: str
    name: str


@dataclass(frozen=True)
class ContactFields:
    """Purpose: group repeated contact fields extracted from Section 3."""

    name: str | None = None
    company: str | None = None
    mailing_address: str | None = None
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class FieldEvidence:
    """Purpose: preserve short field-level evidence for operator review."""

    field_name: str
    value: str | int | None
    evidence_snippet: str
    confidence: float


@dataclass(frozen=True)
class ApplicationExtraction:
    """Purpose: carry normalized Section 3 and Section 5 LLM output."""

    agenda_item_id: int
    source_url: str
    attachment_id: str
    applicant: ContactFields
    project_contact: ContactFields
    owner: ContactFields
    section5_description: str | None
    unit_count: int | None
    status: str
    target_project: bool | None = None
    target_reason: str | None = None
    evidence: tuple[FieldEvidence, ...] = ()


@dataclass(frozen=True)
class DownloadedFile:
    """Purpose: couple a temporary file with its content hash."""

    path: Path
    content_hash: str
    byte_count: int = 0
    content_type: str | None = None
    content_length: int | None = None
    first_bytes: bytes = b""

    def summary(self) -> str:
        """Purpose: format download diagnostics for operator run logs."""

        content_type = self.content_type or "unknown"
        expected = self.content_length if self.content_length is not None else "unknown"
        return (
            f"{self.byte_count} bytes, content_type={content_type}, "
            f"content_length={expected}, sha256={self.content_hash}, "
            f"first_bytes={self.first_bytes.hex()}"
        )


@dataclass(frozen=True)
class RunRequest:
    """Purpose: hold bounded Madison run inputs."""

    date_from: dt.date
    date_to: dt.date
    request_text: str | None = None
