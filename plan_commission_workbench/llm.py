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
        status = application_status(
            {
                "target_project": target_project,
                "section5_description": section5_description,
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
            unit_count=self._int_or_none(payload.get("unit_count")),
            status=status,
            target_project=target_project,
            target_reason=target_reason,
            evidence=evidence,
        )

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
