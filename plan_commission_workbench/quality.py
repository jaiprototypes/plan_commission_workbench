"""Reusable quality checks for extracted applications and contacts."""

from __future__ import annotations

import re
from typing import Any

from . import statuses

CONTACT_PREFIXES = ("applicant", "project_contact", "owner")
RAW_LABEL_WORDS = (
    "applicant name",
    "city/state/zip",
    "email",
    "project contact person",
    "property owner",
    "street address",
    "telephone",
)


def clean_text(value: Any) -> str:
    """Purpose: normalize DB, LLM, and operator text before QC checks."""

    return re.sub(r"\s+", " ", str(value or "")).strip(" ,")


def target_is_true(value: Any) -> bool:
    """Purpose: handle SQLite integers, JSON booleans, and correction strings."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def target_is_false(value: Any) -> bool:
    """Purpose: identify explicit non-target determinations."""

    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no"}
    return False


def application_status(row: dict[str, Any]) -> str:
    """Purpose: keep uncertain extractions out of the clean review queue."""

    if target_is_false(row.get("target_project")):
        return statuses.REJECTED
    if application_quality_issues(row):
        return statuses.NEEDS_OPERATOR_REVIEW
    return statuses.APPLICATION_EXTRACTED


def application_quality_issues(row: dict[str, Any]) -> list[str]:
    """Purpose: explain why a row is not ready for ordinary acceptance."""

    issues: list[str] = []
    if not target_is_true(row.get("target_project")):
        issues.append("Target project is not confirmed")
    if not clean_text(row.get("section5_description")):
        issues.append("Section 5 description is missing")
    if not any(mailable_contact(row, prefix) for prefix in CONTACT_PREFIXES):
        issues.append("No mailable contact has a name or company plus address")
    if row.get("agenda_classification") and row.get("agenda_classification") != statuses.AGENDA_HIT:
        issues.append("Agenda item is not currently classified as a hit")
    raw_issue = first_raw_text_issue(row)
    if raw_issue:
        issues.append(raw_issue)
    return issues


def mailable_contact(row: dict[str, Any], prefix: str) -> tuple[str, str, str] | None:
    """Purpose: accept person+address or company+address contacts for mailing."""

    name = clean_text(row.get(f"{prefix}_name"))
    company = clean_text(row.get(f"{prefix}_company"))
    address = clean_text(row.get(f"{prefix}_mailing_address"))
    if address and (name or company):
        return name, company, address
    return None


def contact_key(name: str, company: str, address: str) -> str:
    """Purpose: dedupe contacts after whitespace and case normalization."""

    value = f"{name}|{company}|{address}".lower()
    return re.sub(r"\W+", " ", value).strip()


def first_raw_text_issue(row: dict[str, Any]) -> str | None:
    """Purpose: catch Docling/form-label fragments before review or export."""

    for prefix in CONTACT_PREFIXES:
        contact = mailable_contact(row, prefix)
        if not contact:
            continue
        raw_issue = raw_text_issue(*contact)
        if raw_issue:
            return raw_issue
    return None


def raw_text_issue(*values: str) -> str | None:
    """Purpose: reject contact values that still contain form labels."""

    joined = " ".join(values).lower()
    for word in RAW_LABEL_WORDS:
        if word in joined:
            return f"contains raw form label text: {word}"
    return None
