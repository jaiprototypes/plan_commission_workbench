"""LLM prompt construction, OpenAI calls, and JSON validation."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from . import statuses
from .exceptions import LLMResponseError
from .models import AgendaClassification, AgendaSegment, ApplicationExtraction, ContactFields, FieldEvidence
from .quality import application_status

JsonResponder = Callable[[str, str], dict[str, Any]]
JSON_TEXT_CONFIG = {"format": {"type": "json_object"}}


def _env_float(name: str, default: float) -> float:
    """Purpose: parse numeric LLM runtime settings without crashing startup."""

    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Purpose: parse integer LLM retry settings without crashing startup."""

    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class AgendaPromptBuilder:
    """Purpose: build batched agenda classification prompts."""

    def build(self, segments: list[AgendaSegment], request_text: str | None = None) -> tuple[str, str]:
        """Purpose: create one JSON-only agenda prompt."""

        guidance = request_text.strip() if request_text else "Use only agenda-description evidence."
        items = [
            {
                "city_item_id": item.city_item_id,
                "file_id": item.file_id,
                "meeting_date": item.meeting_date.isoformat(),
                "description": item.description,
            }
            for item in segments
        ]
        system = (
            "You classify Madison Plan Commission agenda items for a workbench. "
            "LLM interpretation is required. Deterministic code only routed the text. "
            "Classify each item as agenda_hit, not_target_project, or needs_agenda_review. "
            "agenda_hit is only for items that clearly involve multifamily housing buildings, "
            "mixed-use buildings with both residential and commercial space, or office buildings. "
            "Do not mark bare land divisions, Certified Survey Maps, zoning-only changes, "
            "demolition-only items, outdoor storage, landfill, recycling, industrial, warehouse, "
            "single-family, duplex, park, utility, school, church/community-center, or future-lot "
            "items as hits unless the agenda description itself identifies a target building type. "
            "Use needs_agenda_review only when the description strongly hints at a target building "
            "but does not provide enough evidence to classify cleanly. "
            "Return only valid JSON."
        )
        user = json.dumps(
            {
                "run_request_guidance": guidance,
                "required_schema": {
                    "items": [
                        {
                            "city_item_id": "string",
                            "classification": "agenda_hit|not_target_project|needs_agenda_review",
                            "confidence": "0.0-1.0",
                            "reason": "short reason",
                            "evidence_snippet": "short quote or paraphrase from the agenda description",
                        }
                    ]
                },
                "items": items,
            },
            indent=2,
        )
        return system, user


class ApplicationPromptBuilder:
    """Purpose: build Section 3/5 application extraction prompts."""

    def build(self, clipped_text: str) -> tuple[str, str]:
        """Purpose: create one JSON-only application extraction prompt."""

        system = (
            "You extract fields from standardized Madison Land Use Application text. "
            "The input is clipped to Section 3 and Section 5 only. Do not infer fields from outside the text. "
            "Docling may compress form text, remove spaces, or place labels next to values. "
            "Return cleaned conventional contact values: names with normal spacing, company names without labels, "
            "mailing addresses as conventional one-line mailing addresses, phone numbers in conventional readable form, "
            "and email fields only when an actual email address is present. Remove form labels such as Applicant name, "
            "Street address, City/State/Zip, Telephone, Email, Project contact person, and Property owner from values. "
            "Use null when a contact field is blank or only a form label is present. "
            "The workbench target is narrow: multifamily housing buildings, mixed-use buildings "
            "with both commercial and residential space, or office buildings. Set target_project "
            "to false for landfill, recycling, outdoor storage, industrial, warehouse, CSM-only "
            "land divisions, future lots/parcels without a target building, demolition-only, "
            "single-family, duplex, school, church/community-center, park, or infrastructure work. "
            "Return only valid JSON."
        )
        user = json.dumps(
            {
                "required_schema": {
                    "target_project": "boolean|null",
                    "target_reason": "short string explaining target decision",
                    "applicant": CONTACT_SCHEMA,
                    "project_contact": CONTACT_SCHEMA,
                    "owner": CONTACT_SCHEMA,
                    "section5_description": "string|null",
                    "unit_count": "integer|null",
                    "evidence": [
                        {
                            "field_name": "string",
                            "value": "string|integer|null",
                            "evidence_snippet": "short text support",
                            "confidence": "0.0-1.0",
                        }
                    ],
                },
                "section_3_and_5_text": clipped_text,
            },
            indent=2,
        )
        return system, user


class MilwaukeePromptBuilder:
    """Purpose: build Milwaukee CPC evidence-bundle extraction prompts."""

    def build(self, item_context: dict[str, Any], evidence_bundle: str) -> tuple[str, str]:
        """Purpose: create one JSON-only Milwaukee CPC contact prompt."""

        system = (
            "You extract potential client contact fields from Milwaukee City Plan Commission evidence. "
            "The input may combine CPC staff reports, Exhibit A, project narratives, affidavits, plans, "
            "and similar attachments. Do not assume a Madison Land Use Application or fixed form sections. "
            "Find the best available equivalents for applicant, owner, developer, and project contact. "
            "Prefer developer, applicant, and owner entities over public commenters or city staff. "
            "Treat address-named owner LLCs, such as 123 Main Street LLC, as ownership evidence only unless "
            "the document provides a real person, mailing address, phone, or email for that entity. "
            "When affidavits, narratives, or exhibits are included, search them specifically for person names, "
            "mailing addresses, phone numbers, and email addresses that were missing from the staff report. "
            "When plan/title sheets are included, extract owner, general contractor, architect, structural engineer, "
            "and civil engineer contacts only when they are tied to the project team. "
            "Do not attach a mailing address, phone, email, website, or header block from one company to a different "
            "company. If a title sheet lists Pierce, VJS, Kapur, the developer, or another project-team firm in "
            "separate blocks, keep each block as its own contact and leave missing fields null rather than blending "
            "details across companies. "
            "Do not silently repair damaged OCR names from signature lines; if the source text is garbled, "
            "keep confidence low and add a review note. "
            "Use null for missing fields, and keep contact values clean without document labels. "
            "Set target_project true only for multifamily housing, mixed-use buildings with residential/commercial "
            "space, office buildings, or similar development leads. Return only valid JSON."
        )
        user = json.dumps(
            {
                "required_schema": {
                    "target_project": "boolean|null",
                    "target_reason": "short string explaining target decision",
                    "project_name": "string|null",
                    "project_address": "string|null",
                    "unit_count": "integer|null",
                    "contacts": [
                        {
                            "role": "applicant|owner|developer|project_contact|architect|contractor|other",
                            **CONTACT_SCHEMA,
                            "evidence_snippet": "short text support",
                            "confidence": "0.0-1.0",
                        }
                    ],
                    "review_notes": ["short strings for missing or ambiguous fields"],
                    "evidence": [
                        {
                            "field_name": "string",
                            "value": "string|integer|null",
                            "evidence_snippet": "short text support",
                            "confidence": "0.0-1.0",
                        }
                    ],
                },
                "item_context": item_context,
                "evidence_bundle": evidence_bundle,
            },
            indent=2,
        )
        return system, user


class MilwaukeeTriagePromptBuilder:
    """Purpose: build Milwaukee CPC staff-report triage prompts."""

    def build(self, item_context: dict[str, Any], staff_report_text: str) -> tuple[str, str]:
        """Purpose: create one JSON-only project identity and tag prompt."""

        system = (
            "You triage Milwaukee City Plan Commission staff report text before deeper extraction. "
            "Identify whether the item describes a target building lead: multifamily housing, "
            "mixed-use with residential/commercial space, or an office building. "
            "Return a stable project identity so multiple CPC files for the same building can be grouped. "
            "Do not treat street/alley vacations, self-storage/data-processing uses, industrial zoning, "
            "minor modifications, or exterior alterations to existing duplex/single-building residential structures "
            "as target leads unless the staff report itself identifies a target new building. Signage-only changes, "
            "procedural notification rules, use-permission changes, and existing-building exterior work are non-target. "
            "Return only valid JSON."
        )
        user = json.dumps(
            {
                "required_schema": {
                    "target_project": "boolean|null",
                    "target_reason": "short target decision reason",
                    "project_name": "string|null",
                    "project_address": "string|null",
                    "unit_count": "integer|null",
                    "building_type": "multifamily|mixed_use|office|non_target|unknown",
                    "tags": [
                        "target_multifamily|target_mixed_use|target_office|not_target|needs_secondary_documents|staff_report_sufficient|same_building_possible"
                    ],
                    "secondary_documents_needed": "boolean",
                    "secondary_document_reason": "string|null",
                    "confidence": "0.0-1.0",
                    "evidence_snippet": "short text support",
                    "project_candidates": [
                        {
                            "target_project": "boolean|null",
                            "target_reason": "short target decision reason",
                            "project_name": "string|null",
                            "project_address": "string|null",
                            "unit_count": "integer|null",
                            "building_type": "multifamily|mixed_use|office|non_target|unknown",
                            "tags": ["short tag strings"],
                            "secondary_documents_needed": "boolean",
                            "secondary_document_reason": "string|null",
                            "confidence": "0.0-1.0",
                            "evidence_snippet": "short text support",
                        }
                    ],
                },
                "item_context": item_context,
                "staff_report_text": staff_report_text,
            },
            indent=2,
        )
        return system, user


CONTACT_SCHEMA = {
    "name": "clean conventional person name string|null, without form labels",
    "company": "clean conventional company name string|null, without form labels",
    "mailing_address": "clean conventional one-line mailing address string|null, without form labels",
    "phone": "clean conventional phone number string|null, without form labels",
    "email": "actual email address string|null, never a label or adjacent field text",
}


class LLMJsonClient:
    """Purpose: call OpenAI for required JSON classification/extraction."""

    def __init__(self, model: str | None = None, responder: JsonResponder | None = None) -> None:
        self.model = model or os.getenv("PCW_OPENAI_MODEL", "gpt-4.1-mini")
        self.responder = responder
        self.timeout_seconds = max(1.0, _env_float("PCW_OPENAI_TIMEOUT_SECONDS", 180.0))
        self.max_retries = max(0, _env_int("PCW_OPENAI_MAX_RETRIES", 2))
        self.agenda_prompts = AgendaPromptBuilder()
        self.application_prompts = ApplicationPromptBuilder()
        self.milwaukee_prompts = MilwaukeePromptBuilder()
        self.milwaukee_triage_prompts = MilwaukeeTriagePromptBuilder()

    def status(self) -> dict[str, Any]:
        """Purpose: expose UI health for OpenAI configuration."""

        package_available = True
        try:
            import openai  # noqa: F401
        except Exception:
            package_available = False
        return {
            "api_key_present": bool(os.getenv("OPENAI_API_KEY")),
            "package_available": package_available,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }

    def classify_agenda(
        self,
        segments: list[AgendaSegment],
        request_text: str | None = None,
    ) -> list[AgendaClassification]:
        """Purpose: run and validate agenda-sized classification JSON."""

        system, user = self.agenda_prompts.build(segments, request_text)
        payload = self._request_json(system, user)
        return self._validate_agenda(payload, segments)

    def extract_application(
        self,
        agenda_item_id: int,
        source_url: str,
        attachment_id: str,
        clipped_text: str,
    ) -> ApplicationExtraction:
        """Purpose: run and validate application field extraction JSON."""

        system, user = self.application_prompts.build(clipped_text)
        payload = self._request_json(system, user)
        return self._validate_application(agenda_item_id, source_url, attachment_id, payload)

    def extract_milwaukee_contacts(self, item_context: dict[str, Any], evidence_bundle: str) -> dict[str, Any]:
        """Purpose: run and validate Milwaukee CPC contact extraction JSON."""

        system, user = self.milwaukee_prompts.build(item_context, evidence_bundle)
        payload = self._request_json(system, user)
        return self._validate_milwaukee_contacts(payload)

    def triage_milwaukee_staff_report(self, item_context: dict[str, Any], staff_report_text: str) -> dict[str, Any]:
        """Purpose: run and validate Milwaukee staff-report project triage JSON."""

        system, user = self.milwaukee_triage_prompts.build(item_context, staff_report_text)
        payload = self._request_json(system, user)
        return self._validate_milwaukee_triage(payload)

    def _request_json(self, system: str, user: str) -> dict[str, Any]:
        """Purpose: request JSON through an injectable or OpenAI-backed client."""

        if self.responder:
            return self.responder(system, user)
        if not os.getenv("OPENAI_API_KEY"):
            raise LLMResponseError("OPENAI_API_KEY is required for LLM work")
        text = self._openai_text(system, user)
        return self._loads_json(text)

    def _openai_text(self, system: str, user: str) -> str:
        """Purpose: use the installed OpenAI SDK without pinning UI code to it."""

        try:
            from openai import OpenAI
        except Exception as exc:
            raise LLMResponseError("openai package is not installed") from exc
        client = OpenAI(timeout=self.timeout_seconds, max_retries=self.max_retries)
        try:
            response = client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text=JSON_TEXT_CONFIG,
            )
            text = getattr(response, "output_text", None)
            if text:
                return str(text)
        except AttributeError:
            return self._openai_chat_text(client, system, user)
        except Exception as exc:
            raise LLMResponseError(f"OpenAI JSON request failed: {exc}") from exc
        return str(response)

    def _openai_chat_text(self, client: Any, system: str, user: str) -> str:
        """Purpose: keep older SDK/model fallback JSON-constrained too."""

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise LLMResponseError(f"OpenAI chat JSON request failed: {exc}") from exc
        return str(response.choices[0].message.content or "")

    def _loads_json(self, text: str) -> dict[str, Any]:
        """Purpose: parse strict JSON with light markdown-fence cleanup."""

        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"LLM returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMResponseError("LLM JSON must be an object")
        return payload

    def _validate_agenda(
        self,
        payload: dict[str, Any],
        segments: list[AgendaSegment],
    ) -> list[AgendaClassification]:
        """Purpose: validate agenda response shape and statuses."""

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise LLMResponseError("Agenda LLM JSON missing items list")
        expected = {item.city_item_id for item in segments}
        results: list[AgendaClassification] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise LLMResponseError("Agenda LLM item must be an object")
            city_item_id = str(raw.get("city_item_id") or "")
            classification = str(raw.get("classification") or "")
            if city_item_id not in expected:
                raise LLMResponseError(f"Agenda LLM returned unknown city_item_id {city_item_id!r}")
            if classification not in statuses.AGENDA_FINAL_STATUSES:
                raise LLMResponseError(f"Unsupported agenda classification {classification!r}")
            results.append(
                AgendaClassification(
                    city_item_id=city_item_id,
                    classification=classification,
                    confidence=self._confidence(raw.get("confidence")),
                    reason=str(raw.get("reason") or "")[:500],
                    evidence_snippet=str(raw.get("evidence_snippet") or "")[:500],
                )
            )
        if {item.city_item_id for item in results} != expected:
            raise LLMResponseError("Agenda LLM did not classify every item")
        return results

    def _validate_application(
        self,
        agenda_item_id: int,
        source_url: str,
        attachment_id: str,
        payload: dict[str, Any],
    ) -> ApplicationExtraction:
        """Purpose: validate application extraction output."""

        evidence = tuple(self._evidence(item) for item in payload.get("evidence") or [] if isinstance(item, dict))
        target_project = self._bool_or_none(payload.get("target_project"))
        target_reason = self._text_or_none(payload.get("target_reason"))
        applicant = self._contact(payload.get("applicant"))
        project_contact = self._contact(payload.get("project_contact"))
        owner = self._contact(payload.get("owner"))
        section5_description = self._text_or_none(payload.get("section5_description"))
        unit_count = self._int_or_none(payload.get("unit_count"))
        status = application_status(
            {
                "target_project": target_project,
                "section5_description": section5_description,
                "unit_count": unit_count,
                "applicant_name": applicant.name,
                "applicant_company": applicant.company,
                "applicant_mailing_address": applicant.mailing_address,
                "project_contact_name": project_contact.name,
                "project_contact_company": project_contact.company,
                "project_contact_mailing_address": project_contact.mailing_address,
                "owner_name": owner.name,
                "owner_company": owner.company,
                "owner_mailing_address": owner.mailing_address,
            }
        )
        return ApplicationExtraction(
            agenda_item_id=agenda_item_id,
            source_url=source_url,
            attachment_id=attachment_id,
            applicant=applicant,
            project_contact=project_contact,
            owner=owner,
            section5_description=section5_description,
            unit_count=unit_count,
            status=status,
            target_project=target_project,
            target_reason=target_reason,
            evidence=evidence,
        )

    def _validate_milwaukee_contacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Purpose: validate the MVP Milwaukee extraction payload."""

        contacts = payload.get("contacts") or []
        if not isinstance(contacts, list):
            raise LLMResponseError("Milwaukee LLM JSON contacts must be a list")
        notes = payload.get("review_notes") or []
        if not isinstance(notes, list):
            raise LLMResponseError("Milwaukee LLM JSON review_notes must be a list")
        evidence = payload.get("evidence") or []
        if not isinstance(evidence, list):
            raise LLMResponseError("Milwaukee LLM JSON evidence must be a list")
        return {
            "target_project": self._bool_or_none(payload.get("target_project")),
            "target_reason": self._text_or_none(payload.get("target_reason")),
            "project_name": self._text_or_none(payload.get("project_name")),
            "project_address": self._text_or_none(payload.get("project_address")),
            "unit_count": self._int_or_none(payload.get("unit_count")),
            "contacts": [self._milwaukee_contact(item) for item in contacts if isinstance(item, dict)],
            "review_notes": [str(item).strip()[:500] for item in notes if str(item).strip()],
            "evidence": [self._evidence(item).__dict__ for item in evidence if isinstance(item, dict)],
        }

    def _validate_milwaukee_triage(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Purpose: validate the MVP staff-report triage payload."""

        candidates = payload.get("project_candidates")
        if candidates is None:
            candidates = [payload]
        if not isinstance(candidates, list):
            raise LLMResponseError("Milwaukee triage JSON project_candidates must be a list")
        normalized = [self._milwaukee_project_candidate(item) for item in candidates if isinstance(item, dict)]
        primary = normalized[0] if normalized else self._milwaukee_project_candidate(payload)
        return primary | {"project_candidates": normalized or [primary]}

    def _milwaukee_project_candidate(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Purpose: normalize one project candidate from a staff report."""

        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            raise LLMResponseError("Milwaukee triage JSON tags must be a list")
        return {
            "target_project": self._bool_or_none(raw.get("target_project")),
            "target_reason": self._text_or_none(raw.get("target_reason")),
            "project_name": self._text_or_none(raw.get("project_name")),
            "project_address": self._text_or_none(raw.get("project_address")),
            "unit_count": self._int_or_none(raw.get("unit_count")),
            "building_type": self._milwaukee_building_type(raw.get("building_type")),
            "tags": [str(item).strip().lower()[:80] for item in tags if str(item).strip()],
            "secondary_documents_needed": self._bool_or_false(raw.get("secondary_documents_needed")),
            "secondary_document_reason": self._text_or_none(raw.get("secondary_document_reason")),
            "confidence": self._confidence(raw.get("confidence")),
            "evidence_snippet": str(raw.get("evidence_snippet") or "")[:500],
        }

    def _milwaukee_building_type(self, value: Any) -> str:
        """Purpose: constrain triage building labels to known MVP buckets."""

        normalized = str(value or "unknown").strip().lower()
        return normalized if normalized in {"multifamily", "mixed_use", "office", "non_target", "unknown"} else "unknown"

    def _bool_or_false(self, value: Any) -> bool:
        """Purpose: parse optional triage booleans with a conservative default."""

        if value in (None, ""):
            return False
        parsed = self._bool_or_none(value)
        return bool(parsed)

    def _milwaukee_contact(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Purpose: normalize one Milwaukee contact object for the MVP UI."""

        contact = self._contact(raw)
        role = str(raw.get("role") or "other").strip().lower()
        supported_roles = {"applicant", "owner", "developer", "project_contact", "architect", "contractor", "other"}
        if role not in supported_roles:
            role = "other"
        return {
            "role": role,
            "name": contact.name,
            "company": contact.company,
            "mailing_address": contact.mailing_address,
            "phone": contact.phone,
            "email": contact.email,
            "evidence_snippet": str(raw.get("evidence_snippet") or "")[:500],
            "confidence": self._confidence(raw.get("confidence")),
        }

    def _contact(self, raw: Any) -> ContactFields:
        """Purpose: validate a repeated contact object."""

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise LLMResponseError("Contact fields must be objects")
        return ContactFields(
            name=self._text_or_none(raw.get("name")),
            company=self._text_or_none(raw.get("company")),
            mailing_address=self._text_or_none(raw.get("mailing_address")),
            phone=self._text_or_none(raw.get("phone")),
            email=self._text_or_none(raw.get("email")),
        )

    def _evidence(self, raw: dict[str, Any]) -> FieldEvidence:
        """Purpose: validate one field evidence object."""

        field_name = str(raw.get("field_name") or "").strip()
        if not field_name:
            raise LLMResponseError("Evidence field_name is required")
        return FieldEvidence(
            field_name=field_name[:120],
            value=raw.get("value"),
            evidence_snippet=str(raw.get("evidence_snippet") or "")[:500],
            confidence=self._confidence(raw.get("confidence")),
        )

    def _confidence(self, value: Any) -> float:
        """Purpose: clamp model confidence into a sortable float."""

        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    def _text_or_none(self, value: Any) -> str | None:
        """Purpose: normalize optional strings."""

        text = str(value or "").strip()
        return text or None

    def _int_or_none(self, value: Any) -> int | None:
        """Purpose: normalize optional integer fields."""

        if value in (None, ""):
            return None
        try:
            return int(value)
        except Exception as exc:
            raise LLMResponseError(f"unit_count must be an integer or null, got {value!r}") from exc

    def _bool_or_none(self, value: Any) -> bool | None:
        """Purpose: normalize optional model booleans."""

        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lower = value.strip().lower()
            if lower in {"true", "yes", "1"}:
                return True
            if lower in {"false", "no", "0"}:
                return False
        raise LLMResponseError(f"target_project must be boolean or null, got {value!r}")
