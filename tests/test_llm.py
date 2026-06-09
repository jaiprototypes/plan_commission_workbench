from __future__ import annotations

import datetime as dt

import pytest

from plan_commission_workbench import statuses
from plan_commission_workbench.exceptions import LLMResponseError
from plan_commission_workbench.llm import AgendaPromptBuilder, ApplicationPromptBuilder, LLMJsonClient
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
            "applicant": {"name": "Jane Applicant", "company": "Applicant LLC"},
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
