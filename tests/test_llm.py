from __future__ import annotations

import datetime as dt

import pytest

from plan_commission_workbench import statuses
from plan_commission_workbench.exceptions import LLMResponseError
from plan_commission_workbench.llm import (
    AgendaPromptBuilder,
    ApplicationPromptBuilder,
    LLMJsonClient,
    MilwaukeePromptBuilder,
    MilwaukeeTriagePromptBuilder,
)
from plan_commission_workbench.models import AgendaSegment


def _segment(city_item_id: str = "96005") -> AgendaSegment:
    return AgendaSegment(
        event_id="27999",
        city_item_id=city_item_id,
        file_id="88001",
        meeting_date=dt.date(2026, 6, 1),
        description="Conditional Use for a 100-unit apartment building.",
    )


def test_agenda_prompt_includes_request_guidance_and_items() -> None:
    system, user = AgendaPromptBuilder().build([_segment()], "Prefer housing leads.")

    assert "agenda_hit" in system
    assert "multifamily housing" in system
    assert "landfill" in system
    assert "Prefer housing leads." in user
    assert "96005" in user


def test_application_prompt_requires_conventional_contact_formatting() -> None:
    system, user = ApplicationPromptBuilder().build("Section 3 and 5 text")

    assert "cleaned conventional contact values" in system
    assert "mailing addresses as conventional one-line mailing addresses" in system
    assert "Use null when a contact field is blank" in system
    assert "actual email address" in user
    assert "without form labels" in user


def test_milwaukee_prompt_does_not_assume_madison_form_sections() -> None:
    system, user = MilwaukeePromptBuilder().build({"city_file": "252190"}, "CPC Staff Report text")

    assert "Milwaukee City Plan Commission" in system
    assert "Do not assume a Madison Land Use Application" in system
    assert "phone numbers, and email addresses" in system
    assert "plan/title sheets" in system
    assert "leave missing fields null rather than blending details across companies" in system
    assert "address-named owner LLCs" in system
    assert "Do not silently repair damaged OCR names" in system
    assert "developer" in user
    assert "CPC Staff Report text" in user


def test_milwaukee_triage_prompt_identifies_project_group_fields() -> None:
    system, user = MilwaukeeTriagePromptBuilder().build({"city_file": "252190"}, "Staff report text")

    assert "project identity" in system
    assert "street/alley vacations" in system
    assert "Signage-only changes" in system
    assert "secondary_documents_needed" in user
    assert "Staff report text" in user


def test_agenda_llm_validation_requires_every_segment() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "items": [
                {
                    "city_item_id": "96005",
                    "classification": statuses.AGENDA_HIT,
                    "confidence": 0.9,
                    "reason": "Housing construction",
                    "evidence_snippet": "100-unit apartment building",
                }
            ]
        }
    )

    results = client.classify_agenda([_segment()])

    assert results[0].classification == statuses.AGENDA_HIT


def test_agenda_llm_validation_rejects_missing_items() -> None:
    client = LLMJsonClient(responder=lambda _system, _user: {"items": []})

    with pytest.raises(LLMResponseError):
        client.classify_agenda([_segment()])


def test_application_llm_validation_normalizes_contacts_and_evidence() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "target_project": True,
            "target_reason": "Multifamily housing",
            "applicant": {
                "name": "Jane Applicant",
                "company": "Applicant LLC",
                "mailing_address": "123 Main Street, Madison, WI 53703",
            },
            "project_contact": {"email": "pat@example.com"},
            "owner": {},
            "section5_description": "Construct 48 dwelling units.",
            "unit_count": "48",
            "evidence": [
                {
                    "field_name": "unit_count",
                    "value": "48",
                    "evidence_snippet": "48 dwelling units",
                    "confidence": 0.82,
                }
            ],
        }
    )

    extraction = client.extract_application(1, "https://example.test/app.pdf", "171817", "Section text")

    assert extraction.applicant.name == "Jane Applicant"
    assert extraction.project_contact.email == "pat@example.com"
    assert extraction.unit_count == 48
    assert extraction.target_project is True
    assert extraction.status == statuses.APPLICATION_EXTRACTED
    assert extraction.evidence[0].field_name == "unit_count"


def test_application_llm_validation_routes_unknown_target_to_review() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "target_project": None,
            "target_reason": "Section 5 was not clear enough to classify the project.",
            "applicant": {
                "company": "Unclear Development LLC",
                "mailing_address": "123 Main Street, Madison, WI 53703",
            },
            "project_contact": {},
            "owner": {},
            "section5_description": "Project description could not be determined.",
            "unit_count": None,
            "evidence": [],
        }
    )

    extraction = client.extract_application(1, "https://example.test/app.pdf", "171817", "Section text")

    assert extraction.target_project is None
    assert extraction.status == statuses.NEEDS_OPERATOR_REVIEW


def test_application_llm_validation_rejects_non_target_project() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "target_project": False,
            "target_reason": "Landfill recycling and outdoor storage is outside target scope.",
            "applicant": {"name": "Wyeth Augustine-Marceil"},
            "project_contact": {},
            "owner": {},
            "section5_description": "Asphalt shingles recycling program near existing landfill.",
            "unit_count": None,
            "evidence": [],
        }
    )

    extraction = client.extract_application(1, "https://example.test/app.pdf", "182498", "Section text")

    assert extraction.status == statuses.REJECTED
    assert extraction.target_project is False
    assert "Landfill" in extraction.target_reason


def test_application_llm_validation_rejects_bad_unit_count() -> None:
    client = LLMJsonClient(responder=lambda _system, _user: {"unit_count": "forty eight"})

    with pytest.raises(LLMResponseError):
        client.extract_application(1, "https://example.test/app.pdf", "171817", "Section text")


def test_milwaukee_llm_validation_normalizes_contact_payload() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "target_project": True,
            "target_reason": "Staff report describes a multifamily development.",
            "project_name": "The Everett Multifamily",
            "project_address": "234 S Water Street",
            "unit_count": "200",
            "contacts": [
                {
                    "role": "developer",
                    "name": "Jane Developer",
                    "company": "Kaeding Development Group",
                    "mailing_address": "123 Water Street, Milwaukee, WI 53202",
                    "evidence_snippet": "Developer: Kaeding Development Group",
                    "confidence": 0.84,
                }
            ],
            "review_notes": ["Owner phone is missing."],
            "evidence": [
                {
                    "field_name": "unit_count",
                    "value": "200",
                    "evidence_snippet": "200 dwelling units",
                    "confidence": 0.9,
                }
            ],
        }
    )

    extraction = client.extract_milwaukee_contacts({"city_file": "252190"}, "staff report")

    assert extraction["project_name"] == "The Everett Multifamily"
    assert extraction["unit_count"] == 200
    assert extraction["contacts"][0]["role"] == "developer"
    assert extraction["contacts"][0]["company"] == "Kaeding Development Group"
    assert extraction["review_notes"] == ["Owner phone is missing."]


def test_milwaukee_triage_validation_normalizes_project_payload() -> None:
    client = LLMJsonClient(
        responder=lambda _system, _user: {
            "target_project": True,
            "target_reason": "Staff report describes 200 multifamily units.",
            "project_name": "The Everett Multifamily",
            "project_address": "234 S. Water Street, Milwaukee, WI",
            "unit_count": "200",
            "building_type": "multifamily",
            "tags": ["target_multifamily", "staff_report_sufficient"],
            "secondary_documents_needed": False,
            "secondary_document_reason": None,
            "confidence": 0.94,
            "evidence_snippet": "200-unit multi-family residential building",
        }
    )

    triage = client.triage_milwaukee_staff_report({"city_file": "252190"}, "staff report")

    assert triage["target_project"] is True
    assert triage["unit_count"] == 200
    assert triage["building_type"] == "multifamily"
    assert triage["tags"] == ["target_multifamily", "staff_report_sufficient"]
    assert triage["project_candidates"][0]["project_name"] == "The Everett Multifamily"


def test_openai_responses_call_requests_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class FakeResponse:
        output_text = '{"ok": true}'

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: FakeClient())

    payload = LLMJsonClient()._request_json("Return JSON.", "Return JSON.")

    assert payload == {"ok": True}
    assert captured["text"]["format"]["type"] == "json_object"
    assert "verbosity" not in captured["text"]


def test_openai_request_error_is_wrapped_as_llm_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponses:
        def create(self, **_kwargs):
            raise RuntimeError("bad request")

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: FakeClient())

    with pytest.raises(LLMResponseError, match="OpenAI JSON request failed"):
        LLMJsonClient()._request_json("Return JSON.", "Return JSON.")
