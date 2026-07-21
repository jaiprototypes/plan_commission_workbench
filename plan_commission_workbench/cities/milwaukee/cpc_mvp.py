"""Milwaukee CPC discovery, evidence ranking, and MVP extraction."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ...docling_adapter import DoclingTextExtractor
from ...exceptions import DoclingExtractionError, DownloadError, LLMResponseError
from ...legistar import LegistarClient
from ...llm import LLMJsonClient
from ...models import EventRecord

BODY_NAME = "CITY PLAN COMMISSION"
TENANT = "milwaukee"
MAX_BUNDLE_CHARS = 18000
MAX_DOCUMENT_CHARS = 7000
MIN_CANDIDATE_SCORE = 20
DEFAULT_CONTACT_DIG_DOCUMENTS = 2
MAX_CONTACT_DIG_DOCUMENTS = 8
CONTACT_DIRECT_FIELDS = ("mailing_address", "phone", "email")
FAST_TEXT_LAYER_PAGES = 5
MIN_FAST_TEXT_LAYER_CHARS = 200
PRIMARY_OUTREACH_ROLES = {"applicant", "developer", "project_contact"}
SECONDARY_OUTREACH_ROLES = {"architect", "contractor", "other"}
CONTACT_PHONE_RE = re.compile(r"(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}")
CONTACT_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
CITY_BOILERPLATE_PHONE_DIGITS = {"4142865800"}
CITY_EMAIL_DOMAINS = ("@milwaukee.gov",)
PROPERTY_NAMED_OWNER_RE = re.compile(
    r"^\s*\d{1,6}\s+.*\b(street|st\.?|avenue|ave\.?|road|rd\.?|drive|dr\.?|boulevard|blvd\.?|water|main)\b.*\b(l\.?l\.?c\.?|llc)\b",
    re.I,
)

TARGET_TERMS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\bmulti[-\s]?family\b|\bapartments?\b|\bdwelling units?\b", re.I), 45, "multifamily housing language"),
    (re.compile(r"\bmixed[-\s]?use\b", re.I), 40, "mixed-use language"),
    (re.compile(r"\boffice (building|development|space|project)\b|\bcommercial office\b", re.I), 35, "office development language"),
    (re.compile(r"\bresidential\b|\bhousing\b|\bsenior housing\b", re.I), 30, "residential development language"),
    (re.compile(r"\bplanned development\b|\bdetailed planned development\b|\bgeneral planned development\b", re.I), 24, "planned development action"),
    (re.compile(r"\bsite plan review\b|\briverwalk\b|\bdevelopment\b|\bredevelopment\b", re.I), 18, "development review language"),
    (re.compile(r"\bzoning\b|\brezoning\b|\bzoning change\b", re.I), 10, "zoning action with possible development evidence"),
)

NON_TARGET_TERMS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\bminutes\b|\bmeeting details\b|\bregistration\b|\blobbying\b", re.I), -80, "procedural meeting item"),
    (re.compile(r"\bpublic hearing notice\b|\bhearing notice list\b", re.I), -45, "notice-only item"),
    (re.compile(r"\bcomprehensive plan\b|\barea plan\b", re.I), -20, "planning policy item"),
    (re.compile(r"\bstreet vacation\b|\balley vacation\b", re.I), -15, "right-of-way item without building evidence"),
    (re.compile(r"\bvacate\b.*\b(alley|lane|street)\b|\b(alley|lane|street)\b.*\bvacate\b", re.I), -45, "right-of-way vacation item"),
    (re.compile(r"\bnotification requirements?\b|\bzoning map amendments?\b|\bmatters appearing before\b", re.I), -90, "procedural notification item"),
    (re.compile(r"\bminor modification\b.*\bsignage\b|\bsignage\b.*\bminor modification\b|\badditional signage\b", re.I), -95, "signage-only minor modification"),
    (re.compile(r"\bassembly hall\b|\bcatering service\b|\bpermitted uses?\b", re.I), -70, "use-permission item without building lead"),
    (re.compile(r"\bexterior building modifications?\b|\bexisting multi[-\s]?tenant building\b", re.I), -65, "existing-building modification"),
    (re.compile(r"\bto local business\b|\blocal business,\s*lb\d\b|\bcombined with the commercial property\b", re.I), -60, "commercial rezoning without target construction"),
    (re.compile(r"\bindustrial mixed\b|\bfrom industrial heavy\b", re.I), -50, "industrial rezoning without target construction"),
    (re.compile(r"\bindustrial office\b|\bindustrial light\b", re.I), -40, "industrial zoning district language"),
    (re.compile(r"\bself[-\s]?storage\b|\bdata processing\b|\bcomputer services\b|\bcomputational research\b", re.I), -70, "non-target storage or data-processing use"),
    (re.compile(r"\bformer walmart\b|\bpreviously occupied by walmart\b", re.I), -28, "existing big-box reuse signal"),
    (re.compile(r"\bminor modification\b", re.I), -35, "minor modification signal"),
    (re.compile(r"\bexisting building\b|\bexisting residential structure\b|\bexterior alterations\b", re.I), -35, "existing-building alteration signal"),
)

DOCUMENT_SCORES: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\bcpc staff report\b", re.I), 120, "CPC staff report usually summarizes project parties"),
    (re.compile(r"\bstaff report\b", re.I), 110, "staff report usually summarizes project parties"),
    (re.compile(r"\bexhibit a\b", re.I), 92, "Exhibit A often includes owner/developer details"),
    (re.compile(r"\bproject narrative\b|\bdeviation narrative\b", re.I), 86, "narrative often names applicant/developer team"),
    (re.compile(r"\bplan of operation\b", re.I), 76, "plan of operation may expose applicant/operator"),
    (re.compile(r"\baffidavit\b.*\bzoning change\b|\bzoning change\b.*\baffidavit\b", re.I), 64, "zoning affidavit can identify owner/applicant"),
    (re.compile(r"\boverview\b.*\bzoning change\b|\bzoning review matrix\b", re.I), 58, "zoning review document can support contacts"),
    (re.compile(r"\bresolution\b|\bord(inance)?\b", re.I), 22, "legislation text can support project context"),
)

DOCUMENT_EXCLUDES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpublic hearing notice\b|\bhearing notice\b|\bhearing notice list\b", re.I), "notice-only document"),
    (re.compile(r"\bpublic comment\b|\bsupport\b|\bopposition\b|\btestimony\b", re.I), "public comment document"),
    (re.compile(r"\bdrawings?\b|\bplan sheets?\b", re.I), "drawing packet skipped to avoid low-value compute"),
    (re.compile(r"\bmap\b|\brenderings?\b|\bpresentation\b|\bdpw comments\b", re.I), "low-contact visual/comment document"),
    (re.compile(r"\bcity plan commission letter\b", re.I), "post-action city letter"),
)

CONTACT_DIG_SCORES: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\bexhibit a continued\b", re.I), 190, "continued Exhibit A can include title-sheet project team contacts"),
    (re.compile(r"\bexhibit a\b.*\bnarrative\b|\bnarrative\b.*\bexhibit a\b", re.I), 145, "Exhibit A narrative can expose design-team direct details"),
    (re.compile(r"\bproject narrative\b|\bdeviation narrative\b", re.I), 135, "narrative may list applicant or design contacts"),
    (re.compile(r"\baffidavit\b.*\bzoning change\b|\bzoning change\b.*\baffidavit\b", re.I), 120, "affidavit likely has owner/applicant contact details"),
    (re.compile(r"\bplan of operation\b", re.I), 95, "plan of operation may list operator contact details"),
    (re.compile(r"\boverview\b.*\bzoning change\b|\bzoning review matrix\b", re.I), 84, "zoning support document may list applicant details"),
)


@dataclass(frozen=True)
class MilwaukeeEvidenceDocument:
    """Purpose: describe one ranked Milwaukee source attachment."""

    attachment_id: str
    name: str
    source_url: str | None
    score: int
    reason: str
    selected: bool = False
    content_hash: str | None = None
    text_chars: int = 0
    error: str | None = None


@dataclass(frozen=True)
class MilwaukeeCandidateItem:
    """Purpose: carry one Milwaukee CPC item through MVP scoring."""

    event_id: str
    meeting_date: dt.date
    agenda_sequence: str | None
    matter_id: str
    city_file: str | None
    title: str
    candidate_score: int
    candidate_reason: str
    evidence_documents: tuple[MilwaukeeEvidenceDocument, ...]


class MilwaukeeCpcMvpService:
    """Purpose: learn and test where Milwaukee CPC exposes contact fields."""

    def __init__(
        self,
        *,
        legistar: LegistarClient | None = None,
        docling: DoclingTextExtractor | None = None,
        llm: LLMJsonClient | None = None,
        tmp_root: Path | None = None,
    ) -> None:
        self.legistar = legistar or LegistarClient(TENANT)
        self.docling = docling or DoclingTextExtractor()
        self.llm = llm or LLMJsonClient()
        self.tmp_root = tmp_root

    def run_smoke_test(
        self,
        *,
        date_from: dt.date,
        date_to: dt.date,
        max_items: int = 6,
        documents_per_item: int = 1,
        contact_documents_per_project: int = DEFAULT_CONTACT_DIG_DOCUMENTS,
        include_text: bool = False,
        include_llm: bool = False,
    ) -> dict[str, Any]:
        """Purpose: return an in-memory Milwaukee MVP payload for the browser."""

        max_items = max(1, min(max_items, 25))
        documents_per_item = max(1, min(documents_per_item, 5))
        contact_documents_per_project = max(1, min(contact_documents_per_project, MAX_CONTACT_DIG_DOCUMENTS))
        include_text = include_text or include_llm
        events = self.legistar.list_body_events(BODY_NAME, date_from, date_to)
        items = self._candidate_items(events, max_items, documents_per_item)
        if not include_text:
            return self._payload(events, items, include_text=False, include_llm=False)
        tmp_parent = self.tmp_root if self.tmp_root and self.tmp_root.exists() else None
        with tempfile.TemporaryDirectory(prefix="pcw_milwaukee_mvp_", dir=tmp_parent) as tmp_name:
            enriched = [
                self._enrich_item(item, Path(tmp_name), include_llm=include_llm)
                for item in items
            ]
            projects = self._project_groups(enriched) if include_llm else []
            if include_llm:
                self._extract_project_contacts(projects, Path(tmp_name), contact_documents_per_project)
        return self._payload(events, enriched, include_text=True, include_llm=include_llm, projects=projects)

    def _candidate_items(
        self,
        events: list[EventRecord],
        max_items: int,
        documents_per_item: int,
    ) -> list[dict[str, Any]]:
        """Purpose: fetch event items and keep the strongest development signals."""

        candidates: list[MilwaukeeCandidateItem] = []
        for event in events:
            for raw in self.legistar.fetch_event_items(event.event_id):
                candidate = self._candidate_from_raw(event, raw, documents_per_item)
                if candidate and candidate.candidate_score >= MIN_CANDIDATE_SCORE:
                    candidates.append(candidate)
        ordered = sorted(candidates, key=self._candidate_sort_key, reverse=True)
        return [self._item_payload(item) for item in ordered[:max_items]]

    def _candidate_from_raw(
        self,
        event: EventRecord,
        raw: dict[str, Any],
        documents_per_item: int,
    ) -> MilwaukeeCandidateItem | None:
        """Purpose: score one Legistar item without assuming Madison structure."""

        matter_id = str(raw.get("EventItemMatterId") or "").strip()
        if not matter_id:
            return None
        title = self._item_title(raw)
        documents = self.rank_evidence_documents(raw, documents_per_item)
        doc_score = max((document.score for document in documents), default=0)
        candidate_score, reason = self._candidate_score(title, doc_score)
        return MilwaukeeCandidateItem(
            event_id=event.event_id,
            meeting_date=event.meeting_date,
            agenda_sequence=self._text_or_none(raw.get("EventItemAgendaSequence")),
            matter_id=matter_id,
            city_file=self._text_or_none(raw.get("EventItemMatterFile")),
            title=title,
            candidate_score=candidate_score,
            candidate_reason=reason,
            evidence_documents=tuple(documents),
        )

    def rank_evidence_documents(
        self,
        raw_item: dict[str, Any],
        documents_per_item: int = 3,
    ) -> list[MilwaukeeEvidenceDocument]:
        """Purpose: rank Milwaukee attachments by likely contact evidence value."""

        ranked = [self._rank_attachment(raw_item, raw) for raw in raw_item.get("EventItemMatterAttachments") or []]
        useful = sorted((item for item in ranked if item.score > 0), key=lambda item: item.score, reverse=True)
        selected_ids = {item.attachment_id for item in useful[:documents_per_item]}
        return [
            MilwaukeeEvidenceDocument(**(asdict(item) | {"selected": item.attachment_id in selected_ids}))
            for item in useful
        ]

    def _rank_attachment(self, raw_item: dict[str, Any], raw: dict[str, Any]) -> MilwaukeeEvidenceDocument:
        """Purpose: score one attachment by filename and direct download availability."""

        name = str(raw.get("MatterAttachmentName") or "").strip() or "Attachment"
        source_url = self._attachment_url(raw_item, raw)
        if not source_url or not source_url.lower().endswith(".pdf"):
            return MilwaukeeEvidenceDocument(str(raw.get("MatterAttachmentId") or ""), name, source_url, 0, "not a direct PDF")
        for pattern, reason in DOCUMENT_EXCLUDES:
            if pattern.search(name):
                return MilwaukeeEvidenceDocument(str(raw.get("MatterAttachmentId") or ""), name, source_url, 0, reason)
        score, reason = 0, "low contact signal"
        for pattern, value, pattern_reason in DOCUMENT_SCORES:
            if pattern.search(name) and value > score:
                score, reason = value, pattern_reason
        score, reason = self._adjust_document_score(name, score, reason)
        return MilwaukeeEvidenceDocument(str(raw.get("MatterAttachmentId") or ""), name, source_url, score, reason)

    def _adjust_document_score(self, name: str, score: int, reason: str) -> tuple[int, str]:
        """Purpose: prefer current primary evidence over duplicate continuations."""

        lower = name.lower()
        if "continued" in lower:
            score -= 30
            reason = f"{reason}; continuation is secondary evidence"
        if "previous" in lower:
            score -= 35
            reason = f"{reason}; previous filing is secondary evidence"
        return max(score, 0), reason

    def _attachment_url(self, raw_item: dict[str, Any], raw: dict[str, Any]) -> str | None:
        """Purpose: prefer Milwaukee's direct attachment URL and keep API fallback."""

        direct = self._text_or_none(raw.get("MatterAttachmentHyperlink"))
        if direct:
            return direct
        matter_id = self._text_or_none(raw_item.get("EventItemMatterId"))
        attachment_id = self._text_or_none(raw.get("MatterAttachmentId"))
        if matter_id and attachment_id:
            return f"{self.legistar.base_url}/Matters/{matter_id}/Attachments/{attachment_id}/File"
        return None

    def _candidate_score(self, title: str, doc_score: int) -> tuple[int, str]:
        """Purpose: combine item text and source-document signals cautiously."""

        score = 0
        reasons: list[str] = []
        for pattern, value, reason in TARGET_TERMS:
            if pattern.search(title):
                score += value
                reasons.append(reason)
        for pattern, value, reason in NON_TARGET_TERMS:
            if pattern.search(title):
                score += value
                reasons.append(reason)
        if score > 0 and doc_score >= 90:
            score += 12
            reasons.append("strong evidence document available")
        return score, "; ".join(dict.fromkeys(reasons)) or "weak development signal"

    def _enrich_item(self, item: dict[str, Any], tmp_dir: Path, *, include_llm: bool) -> dict[str, Any]:
        """Purpose: download selected documents, extract text, and optionally triage."""

        bundle_parts: list[str] = []
        documents: list[dict[str, Any]] = []
        for document in item["evidence_documents"]:
            enriched, text = self._extract_document_text(item, document, tmp_dir)
            documents.append(enriched)
            if text:
                bundle_parts.append(self._bundle_part(enriched, text))
        item = {**item, "evidence_documents": documents}
        evidence_bundle = "\n\n".join(bundle_parts)[:MAX_BUNDLE_CHARS]
        item["_evidence_bundle"] = evidence_bundle
        item["evidence_bundle_chars"] = len(evidence_bundle)
        if include_llm:
            item["staff_report_triage"] = self._triage_staff_report(item, evidence_bundle)
        return item

    def _triage_staff_report(self, item: dict[str, Any], evidence_bundle: str) -> dict[str, Any]:
        """Purpose: identify target project identity before deeper extraction."""

        if not evidence_bundle.strip():
            return {"error": "No text was extracted from selected evidence documents"}
        context = self._item_context(item)
        try:
            return self.llm.triage_milwaukee_staff_report(context, evidence_bundle)
        except LLMResponseError as exc:
            return {"error": str(exc)}

    def _project_groups(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Purpose: collapse multiple CPC actions that describe one building."""

        groups: dict[str, dict[str, Any]] = {}
        for item in items:
            for candidate in self._triage_project_candidates(item):
                key = self._project_key(item, candidate)
                groups.setdefault(key, self._new_project_group(key, item, candidate))
                self._append_project_item(groups[key], item, candidate)
        projects = list(groups.values())
        for project in projects:
            self._finalize_project_group(project)
        return sorted(projects, key=self._project_sort_key, reverse=True)

    def _triage_project_candidates(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """Purpose: support staff reports that describe multiple project leads."""

        triage = item.get("staff_report_triage") or {}
        raw_candidates = triage.get("project_candidates") or []
        candidates = [candidate for candidate in raw_candidates if isinstance(candidate, dict)]
        return candidates or [triage]

    def _project_key(self, item: dict[str, Any], candidate: dict[str, Any]) -> str:
        """Purpose: build a stable key from staff-report project identity."""

        address = self._identity_token(candidate.get("project_address"))
        units = candidate.get("unit_count")
        name = self._identity_token(candidate.get("project_name"))
        if address and units:
            return f"address:{address}|units:{units}"
        if address and self._should_group_by_address_only(candidate):
            return f"address:{address}"
        if name and address:
            return f"name:{name}|address:{address}"
        if name:
            return f"name:{name}"
        if address:
            return f"address:{address}"
        return f"matter:{item.get('matter_id')}"

    def _should_group_by_address_only(self, candidate: dict[str, Any]) -> bool:
        """Purpose: merge Milwaukee companion office actions when names are inconsistent."""

        if candidate.get("unit_count"):
            return False
        building_type = self._identity_token(candidate.get("building_type"))
        project_name = self._identity_token(candidate.get("project_name"))
        tags = " ".join(self._identity_token(tag) for tag in candidate.get("tags") or [])
        return "office" in {building_type, *project_name.split(), *tags.split()}

    def _new_project_group(self, key: str, item: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        """Purpose: seed one project group from the first matching item."""

        return {
            "project_key": key,
            "project_name": candidate.get("project_name"),
            "project_address": candidate.get("project_address"),
            "unit_count": candidate.get("unit_count"),
            "building_type": candidate.get("building_type"),
            "target_project": candidate.get("target_project"),
            "target_reason": candidate.get("target_reason"),
            "confidence": candidate.get("confidence") or 0,
            "tags": set(candidate.get("tags") or []),
            "related_files": [],
            "items": [],
            "representative_item": item,
            "representative_candidate": candidate,
        }

    def _append_project_item(self, project: dict[str, Any], item: dict[str, Any], candidate: dict[str, Any]) -> None:
        """Purpose: attach one CPC action to a project group."""

        if candidate.get("confidence", 0) >= project.get("confidence", 0):
            project["representative_item"] = item
            project["representative_candidate"] = candidate
            for field in ("project_name", "project_address", "unit_count", "building_type", "target_project", "target_reason", "confidence"):
                if candidate.get(field) not in (None, ""):
                    project[field] = candidate.get(field)
        project["tags"].update(candidate.get("tags") or [])
        project["items"].append(item)
        project["related_files"].append(
            {
                "city_file": item.get("city_file"),
                "matter_id": item.get("matter_id"),
                "event_id": item.get("event_id"),
                "meeting_date": item.get("meeting_date"),
                "score": item.get("candidate_score"),
                "title": item.get("title"),
                "project_name": candidate.get("project_name"),
                "target_project": candidate.get("target_project"),
            }
        )

    def _finalize_project_group(self, project: dict[str, Any]) -> None:
        """Purpose: convert internal sets and add project-level duplicate tags."""

        if len(project["items"]) > 1:
            project["tags"].update({"same_building_multiple_actions", "duplicate_agenda_item"})
        if project.get("target_project") is False:
            project["tags"].add("not_target")
        project["tags"] = sorted(project["tags"])
        project["item_count"] = len(project["items"])

    def _extract_project_contacts(
        self,
        projects: list[dict[str, Any]],
        tmp_dir: Path,
        contact_documents_per_project: int = DEFAULT_CONTACT_DIG_DOCUMENTS,
    ) -> None:
        """Purpose: run deeper contact extraction once per grouped target project."""

        for project in projects:
            if project.get("target_project") is False:
                continue
            item = project.get("representative_item") or {}
            evidence_bundle = str(item.get("_evidence_bundle") or "")
            extraction = self._extract_contacts(item, evidence_bundle)
            project["initial_extraction"] = extraction
            if self._needs_contact_dig(extraction):
                secondary_bundle, documents = self._secondary_contact_bundle(project, tmp_dir, contact_documents_per_project)
                if secondary_bundle.strip():
                    self._add_project_tag(project, "secondary_contact_dig")
                    project["contact_dig_documents"] = documents
                    extraction = self._extract_contacts(item, self._secondary_contact_evidence(project, extraction, secondary_bundle))
            project["extraction"] = extraction
            project["contacts"] = extraction.get("contacts") if isinstance(extraction, dict) else []
            self._attach_contact_search_verification(project, extraction)

    def _secondary_contact_evidence(
        self,
        project: dict[str, Any],
        initial_extraction: dict[str, Any],
        secondary_bundle: str,
    ) -> str:
        """Purpose: avoid resending full staff reports during secondary contact extraction."""

        context = {
            "project_name": project.get("project_name") or initial_extraction.get("project_name"),
            "project_address": project.get("project_address") or initial_extraction.get("project_address"),
            "unit_count": project.get("unit_count") or initial_extraction.get("unit_count"),
            "building_type": project.get("building_type"),
            "target_reason": project.get("target_reason") or initial_extraction.get("target_reason"),
            "related_files": [row.get("city_file") for row in project.get("related_files") or []],
            "contacts_missing_detail": [
                {
                    "role": contact.get("role"),
                    "name": contact.get("name"),
                    "company": contact.get("company"),
                    "outreach_priority": contact.get("outreach_priority"),
                }
                for contact in initial_extraction.get("contacts") or []
                if isinstance(contact, dict) and not self._is_useful_outreach_contact(contact)
            ],
        }
        return (
            "PROJECT CONTEXT FROM STAFF REPORT:\n"
            f"{json.dumps(context, indent=2)}\n\n"
            "SECONDARY CONTACT EVIDENCE:\n"
            f"{secondary_bundle}"
        )[:MAX_BUNDLE_CHARS]

    def _needs_contact_dig(self, extraction: dict[str, Any]) -> bool:
        """Purpose: decide when extraction still lacks useful outreach contacts."""

        if extraction.get("error"):
            return False
        contacts = extraction.get("contacts") or []
        if not contacts:
            return True
        return not any(self._is_useful_outreach_contact(contact) for contact in contacts if isinstance(contact, dict))

    def _attach_contact_search_verification(self, project: dict[str, Any], extraction: dict[str, Any]) -> None:
        """Purpose: make missing Milwaukee contacts auditable instead of silent."""

        if extraction.get("error"):
            project["contact_search_verification"] = self._contact_search_verification(project, "contact_extraction_error")
            project["external_enrichment_candidates"] = self._external_enrichment_candidates(project)
            return
        status = self._project_contact_status(project)
        project["contact_search_verification"] = self._contact_search_verification(project, status)
        project["external_enrichment_candidates"] = self._external_enrichment_candidates(project)

    def _project_contact_status(self, project: dict[str, Any]) -> str:
        """Purpose: require a direct primary route before calling a project solved."""

        if self._project_primary_contact_count(project):
            return "primary_contact_found"
        if self._project_useful_contact_count(project):
            return "project_team_contact_only"
        return "no_direct_contact_found"

    def _project_useful_contact_count(self, project: dict[str, Any]) -> int:
        """Purpose: count outreach-usable rows after all project contact passes."""

        return sum(
            1
            for contact in project.get("contacts") or []
            if isinstance(contact, dict) and contact.get("useful_for_outreach") is True
        )

    def _project_primary_contact_count(self, project: dict[str, Any]) -> int:
        """Purpose: count direct developer/applicant/project-contact rows."""

        return sum(
            1
            for contact in project.get("contacts") or []
            if isinstance(contact, dict) and contact.get("outreach_priority") == "primary"
        )

    def _external_enrichment_candidates(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """Purpose: queue CPC-identified parties that need off-CPC contact lookup."""

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for contact in project.get("contacts") or []:
            if not isinstance(contact, dict) or not self._needs_external_enrichment(contact):
                continue
            key = (
                self._identity_token(contact.get("role")),
                self._identity_token(contact.get("company")),
                self._identity_token(contact.get("name")),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(self._external_enrichment_candidate(project, contact))
        return rows

    def _needs_external_enrichment(self, contact: dict[str, Any]) -> bool:
        """Purpose: identify likely client parties with no direct route in CPC PDFs."""

        if self._is_owner_entity_only(contact):
            return False
        role = str(contact.get("role") or "").strip().lower()
        if role not in {*PRIMARY_OUTREACH_ROLES, "owner"}:
            return False
        if self._has_direct_contact_detail(contact):
            return False
        return bool(str(contact.get("company") or contact.get("name") or "").strip())

    def _external_enrichment_candidate(self, project: dict[str, Any], contact: dict[str, Any]) -> dict[str, Any]:
        """Purpose: shape one future lookup target from CPC evidence."""

        return {
            "role": contact.get("role"),
            "company": contact.get("company"),
            "name": contact.get("name"),
            "project_key": project.get("project_key"),
            "project_name": project.get("project_name"),
            "project_address": project.get("project_address"),
            "unit_count": project.get("unit_count"),
            "reason": "CPC documents identify this party but do not publish a direct contact route.",
            "evidence_snippet": contact.get("evidence_snippet"),
        }

    def _contact_search_verification(self, project: dict[str, Any], status: str) -> dict[str, Any]:
        """Purpose: record exactly which source PDFs were checked for contacts."""

        documents = self._checked_contact_documents(project)
        return {
            "status": status,
            "useful_contact_count": self._project_useful_contact_count(project),
            "primary_contact_count": self._project_primary_contact_count(project),
            "checked_document_count": len(documents),
            "checked_documents": [
                self._contact_search_document_row(document, status)
                for document in documents
            ],
        }

    def _checked_contact_documents(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """Purpose: collect initial and fallback documents actually extracted."""

        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in project.get("items") or []:
            for document in item.get("evidence_documents") or []:
                if not document.get("selected") or not self._document_was_checked(document):
                    continue
                self._append_unique_checked_document(documents, seen, document | self._tracking_item_fields(item) | {"document_role": "initial"})
        for document in project.get("contact_dig_documents") or []:
            if self._document_was_checked(document):
                self._append_unique_checked_document(documents, seen, document | {"document_role": "contact_dig"})
        return documents

    def _document_was_checked(self, document: dict[str, Any]) -> bool:
        """Purpose: separate real extraction attempts from merely ranked files."""

        return bool(document.get("content_hash") or document.get("error") or document.get("text_chars"))

    def _append_unique_checked_document(
        self,
        rows: list[dict[str, Any]],
        seen: set[str],
        document: dict[str, Any],
    ) -> None:
        """Purpose: avoid duplicate verification rows for repeated CPC actions."""

        key = str(document.get("source_url") or document.get("attachment_id") or document.get("name") or "")
        if key in seen:
            return
        seen.add(key)
        rows.append(document)

    def _contact_search_document_row(self, document: dict[str, Any], status: str) -> dict[str, Any]:
        """Purpose: summarize one checked PDF without returning extracted text."""

        signals = self._document_direct_contact_signals(document)
        return {
            "document_role": document.get("document_role"),
            "city_file": document.get("city_file"),
            "matter_id": document.get("matter_id"),
            "document_name": document.get("name"),
            "document_family": self._contact_document_family(document.get("name")),
            "attachment_id": document.get("attachment_id"),
            "source_url": document.get("source_url"),
            "content_hash": document.get("content_hash"),
            "docling_mode": document.get("docling_mode"),
            "text_chars": document.get("text_chars"),
            "error": document.get("error"),
            "phone_signal_count": signals["phone_count"],
            "email_signal_count": signals["email_count"],
            "city_boilerplate_phone_count": signals["city_boilerplate_phone_count"],
            "checked_result": self._contact_search_document_result(document, signals, status),
        }

    def _document_direct_contact_signals(self, document: dict[str, Any]) -> dict[str, int]:
        """Purpose: detect cheap phone/email evidence while ignoring city boilerplate."""

        text = str(document.get("_tracking_text") or "")
        phones = CONTACT_PHONE_RE.findall(text)
        emails = CONTACT_EMAIL_RE.findall(text)
        return {
            "phone_count": sum(1 for phone in phones if self._phone_digits(phone) not in CITY_BOILERPLATE_PHONE_DIGITS),
            "email_count": sum(1 for email in emails if not self._is_city_email(email)),
            "city_boilerplate_phone_count": sum(1 for phone in phones if self._phone_digits(phone) in CITY_BOILERPLATE_PHONE_DIGITS),
        }

    def _contact_search_document_result(
        self,
        document: dict[str, Any],
        signals: dict[str, int],
        status: str,
    ) -> str:
        """Purpose: explain why a checked document did or did not produce a lead."""

        if document.get("error"):
            return "extraction_error"
        if not document.get("text_chars"):
            return "no_text_extracted"
        if signals["phone_count"] or signals["email_count"]:
            if status == "primary_contact_found":
                return "direct_contact_signal_found"
            if status == "project_team_contact_only":
                return "project_team_direct_signal_only"
            return "direct_signal_present_but_not_promoted_to_project_contact"
        if signals["city_boilerplate_phone_count"]:
            return "city_boilerplate_contact_only"
        return "no_direct_phone_or_email_signal_detected"

    def _phone_digits(self, value: Any) -> str:
        """Purpose: compare phone strings after OCR punctuation noise."""

        return re.sub(r"\D+", "", str(value or ""))

    def _is_city_email(self, value: Any) -> bool:
        """Purpose: avoid counting Milwaukee staff inboxes as developer leads."""

        email = str(value or "").strip().lower()
        return any(email.endswith(domain) for domain in CITY_EMAIL_DOMAINS)

    def _add_project_tag(self, project: dict[str, Any], tag: str) -> None:
        """Purpose: append project tags after grouping has finalized tag shape."""

        tags = project.get("tags")
        if isinstance(tags, set):
            tags.add(tag)
            return
        values = set(tags or [])
        values.add(tag)
        project["tags"] = sorted(values)

    def _secondary_contact_bundle(
        self,
        project: dict[str, Any],
        tmp_dir: Path,
        document_limit: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Purpose: extract bounded secondary evidence when contact details are missing."""

        parts: list[str] = []
        documents: list[dict[str, Any]] = []
        for item, document in self._contact_dig_documents(project, document_limit):
            enriched, text = self._extract_document_text(item, document | {"selected": True}, tmp_dir)
            documents.append(enriched | self._tracking_item_fields(item))
            if text:
                parts.append(self._bundle_part(enriched, text))
            if len(parts) >= document_limit:
                break
        return "\n\n".join(parts), documents

    def _contact_dig_documents(
        self,
        project: dict[str, Any],
        document_limit: int = DEFAULT_CONTACT_DIG_DOCUMENTS,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Purpose: rank unselected related documents for contact detail recovery."""

        ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        seen_urls: set[str] = set()
        for item in project.get("items") or []:
            for document in item.get("evidence_documents") or []:
                source_url = str(document.get("source_url") or "")
                if not source_url or source_url in seen_urls or document.get("selected"):
                    continue
                seen_urls.add(source_url)
                score, reason = self._contact_dig_score(str(document.get("name") or ""))
                if score <= 0:
                    continue
                ranked.append((score, item, document | {"contact_dig_reason": reason}))
        ranked.sort(key=lambda row: row[0], reverse=True)
        limit = max(1, min(document_limit, MAX_CONTACT_DIG_DOCUMENTS))
        deduped = self._dedupe_contact_document_scores(ranked)
        return [(item, document | {"contact_dig_score": score}) for score, item, document in deduped[:limit]]

    def _dedupe_contact_document_scores(
        self,
        ranked: list[tuple[int, dict[str, Any], dict[str, Any]]],
    ) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
        """Purpose: avoid spending the contact pass on duplicate related packets."""

        seen_families: set[str] = set()
        rows: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for score, item, document in ranked:
            family_key = self._contact_document_family(document.get("name"))
            if family_key in seen_families:
                continue
            seen_families.add(family_key)
            rows.append((score, item, document))
        return sorted(rows, key=lambda row: row[0], reverse=True)

    def _contact_document_family(self, name: Any) -> str:
        """Purpose: treat dated copies of the same Milwaukee packet as one source."""

        token = self._identity_token(re.sub(r"\bas of\b\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", " ", str(name or ""), flags=re.I))
        token = re.sub(r"\bpdf\b$", " ", token).strip()
        if "exhibit a continued" in token:
            return "exhibit a continued"
        if "affidavit" in token and "zoning change" in token:
            return "affidavit zoning change"
        return token

    def _is_drawing_document(self, name: Any) -> bool:
        """Purpose: count expensive visual packets excluded from Milwaukee contact work."""

        return bool(re.search(r"\bdrawings?\b|\bplan sheets?\b", str(name or ""), re.I))

    def _contact_dig_score(self, name: str) -> tuple[int, str]:
        """Purpose: score secondary documents by contact-detail likelihood."""

        score, reason = 0, "low contact-detail signal"
        for pattern, value, pattern_reason in CONTACT_DIG_SCORES:
            if pattern.search(name) and value > score:
                score, reason = value, pattern_reason
        score, reason = self._adjust_document_score(name, score, reason)
        return score, reason

    def _extract_document_text(
        self,
        item: dict[str, Any],
        document: dict[str, Any],
        tmp_dir: Path,
    ) -> tuple[dict[str, Any], str]:
        """Purpose: convert one selected PDF while keeping failures visible."""

        if not document.get("selected") or not document.get("source_url"):
            return document, ""
        doc_dir = tmp_dir / f"docling_{item['matter_id']}_{document['attachment_id']}"
        pdf_path = tmp_dir / f"{self._safe_name(item['matter_id'])}_{self._safe_name(document['attachment_id'])}.pdf"
        try:
            downloaded = self.legistar.download_file(str(document["source_url"]), pdf_path)
            fast_text = self._fast_text_layer(downloaded.path, document)
            if fast_text:
                text = fast_text[:MAX_DOCUMENT_CHARS]
                return document | {"content_hash": downloaded.content_hash, "text_chars": len(fast_text), "docling_mode": "text_layer", "_tracking_text": text}, text
            result = self.docling.extract_pdf_text_result(downloaded.path, doc_dir)
            text = result.text[:MAX_DOCUMENT_CHARS]
            return document | {"content_hash": downloaded.content_hash, "text_chars": len(result.text), "docling_mode": result.mode, "_tracking_text": text}, text
        except (DownloadError, DoclingExtractionError) as exc:
            return document | {"error": str(exc)}, ""
        finally:
            pdf_path.unlink(missing_ok=True)
            shutil.rmtree(doc_dir, ignore_errors=True)

    def _fast_text_layer(self, pdf_path: Path, document: dict[str, Any]) -> str:
        """Purpose: read Milwaukee text-layer PDFs without slow visual conversion."""

        if not self._should_use_fast_text_layer(document):
            return ""
        try:
            return self._extract_pdfium_text(pdf_path, FAST_TEXT_LAYER_PAGES)
        except Exception:
            return ""

    def _should_use_fast_text_layer(self, document: dict[str, Any]) -> bool:
        """Purpose: keep the fast path scoped to verified text-heavy packets."""

        return bool(
            re.search(
                r"\bcpc staff report\b|\bstaff report\b|\bexhibit a continued\b|\bexhibit a\b.*\bnarrative\b|\bproject narrative\b|\bdeviation narrative\b",
                str(document.get("name") or ""),
                re.I,
            )
        )

    def _extract_pdfium_text(self, pdf_path: Path, max_pages: int) -> str:
        """Purpose: extract embedded text from the first pages of a PDF."""

        try:
            import pypdfium2 as pdfium
        except Exception:
            return ""
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            parts: list[str] = []
            for index in range(min(len(pdf), max_pages)):
                page = pdf[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        parts.append(text_page.get_text_range())
                    finally:
                        text_page.close()
                finally:
                    page.close()
            text = "\n".join(parts).strip()
            return text if len(text) >= MIN_FAST_TEXT_LAYER_CHARS else ""
        finally:
            pdf.close()

    def _bundle_part(self, document: dict[str, Any], text: str) -> str:
        """Purpose: keep document provenance attached to each text excerpt."""

        return (
            f"DOCUMENT: {document.get('name')}\n"
            f"ATTACHMENT_ID: {document.get('attachment_id')}\n"
            f"SOURCE_URL: {document.get('source_url')}\n"
            f"CONTENT_HASH: {document.get('content_hash')}\n"
            f"TEXT:\n{text}"
        )

    def _extract_contacts(self, item: dict[str, Any], evidence_bundle: str) -> dict[str, Any]:
        """Purpose: ask the Milwaukee-specific prompt for normalized contact fields."""

        if not evidence_bundle.strip():
            return {"error": "No text was extracted from selected evidence documents"}
        context = self._item_context(item)
        try:
            return self._flag_contact_quality(self.llm.extract_milwaukee_contacts(context, evidence_bundle))
        except LLMResponseError as exc:
            return {"error": str(exc)}

    def _flag_contact_quality(self, extraction: dict[str, Any]) -> dict[str, Any]:
        """Purpose: mark contacts that need human review before outreach use."""

        if extraction.get("error"):
            return extraction
        flags: list[str] = []
        for contact in extraction.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            contact["outreach_priority"] = self._outreach_priority(contact)
            contact["useful_for_outreach"] = self._is_useful_outreach_contact(contact)
            contact_flags = self._contact_review_flags(contact)
            if not contact_flags:
                continue
            contact["review_flags"] = sorted(set([*contact.get("review_flags", []), *contact_flags]))
            flags.extend(contact_flags)
        flags.extend(self._demote_duplicate_primary_company_contacts(extraction.get("contacts") or []))
        if "ownership_evidence_only" in flags:
            self._append_review_note(extraction, "Address-named owner LLCs are ownership evidence only unless paired with a real contact.")
        if "manual_verify_affidavit_signature_ocr" in flags:
            self._append_review_note(extraction, "Verify OCR-derived affidavit signature names before using them for outreach.")
        if "manual_verify_phone_ocr" in flags:
            self._append_review_note(extraction, "Verify nonstandard OCR-derived phone numbers before using them for outreach.")
        if "duplicate_company_contact_detail" in flags:
            self._append_review_note(extraction, "Review duplicate primary contacts for the same company before outreach.")
        return extraction

    def _demote_duplicate_primary_company_contacts(self, contacts: list[Any]) -> list[str]:
        """Purpose: avoid multiple primary lead rows for one Milwaukee company."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for contact in contacts:
            if not isinstance(contact, dict) or contact.get("outreach_priority") != "primary":
                continue
            company_key = self._identity_token(contact.get("company"))
            if company_key:
                grouped.setdefault(company_key, []).append(contact)
        flags: list[str] = []
        for rows in grouped.values():
            if len(rows) < 2:
                continue
            keep = max(rows, key=self._primary_contact_quality_key)
            for contact in rows:
                if contact is keep:
                    continue
                contact["outreach_priority"] = "duplicate_company_contact_detail"
                contact["useful_for_outreach"] = False
                contact["review_flags"] = sorted(set([*contact.get("review_flags", []), "duplicate_company_contact_detail"]))
                flags.append("duplicate_company_contact_detail")
        return flags

    def _primary_contact_quality_key(self, contact: dict[str, Any]) -> tuple[int, int, float]:
        """Purpose: keep the strongest outreach row when a company is duplicated."""

        role_rank = {"developer": 3, "project_contact": 2, "applicant": 1}
        role = str(contact.get("role") or "").strip().lower()
        detail_count = sum(1 for field in CONTACT_DIRECT_FIELDS if str(contact.get(field) or "").strip())
        return role_rank.get(role, 0), detail_count, float(contact.get("confidence") or 0)

    def _contact_review_flags(self, contact: dict[str, Any]) -> list[str]:
        """Purpose: detect OCR-risky contact evidence without hiding it."""

        flags: list[str] = []
        if self._is_owner_entity_only(contact):
            flags.append("ownership_evidence_only")
        snippet = str(contact.get("evidence_snippet") or "").lower()
        if "petitioner(signature)" in snippet or "petitioner (signature)" in snippet:
            has_direct_contact = any(str(contact.get(field) or "").strip() for field in CONTACT_DIRECT_FIELDS)
            if not has_direct_contact:
                flags.append("manual_verify_affidavit_signature_ocr")
        if self._phone_needs_review(contact.get("phone")):
            flags.append("manual_verify_phone_ocr")
        return flags

    def _outreach_priority(self, contact: dict[str, Any]) -> str:
        """Purpose: separate real lead contacts from ownership evidence."""

        if self._is_owner_entity_only(contact):
            return "ownership_evidence_only"
        role = str(contact.get("role") or "").strip().lower()
        if role in PRIMARY_OUTREACH_ROLES and self._has_direct_contact_detail(contact):
            return "primary"
        if not role and self._has_direct_contact_detail(contact):
            return "secondary_project_team"
        if role in SECONDARY_OUTREACH_ROLES and self._has_direct_contact_detail(contact):
            return "secondary_project_team"
        if role == "owner" and self._has_direct_contact_detail(contact):
            return "owner_review"
        return "needs_contact_detail"

    def _is_useful_outreach_contact(self, contact: dict[str, Any]) -> bool:
        """Purpose: define the scalable Milwaukee lead-contact threshold."""

        return self._outreach_priority(contact) in {"primary", "secondary_project_team", "owner_review"}

    def _is_owner_entity_only(self, contact: dict[str, Any]) -> bool:
        """Purpose: identify address-named owner LLCs that are not outreach leads."""

        if str(contact.get("role") or "").strip().lower() != "owner":
            return False
        company = str(contact.get("company") or "").strip()
        if not company or not PROPERTY_NAMED_OWNER_RE.search(company):
            return False
        return not any(str(contact.get(field) or "").strip() for field in ("name", *CONTACT_DIRECT_FIELDS))

    def _has_direct_contact_detail(self, contact: dict[str, Any]) -> bool:
        """Purpose: require a usable route to reach a lead."""

        return any(str(contact.get(field) or "").strip() for field in CONTACT_DIRECT_FIELDS)

    def _phone_needs_review(self, value: Any) -> bool:
        """Purpose: flag phone strings that are not standard US-length numbers."""

        digits = re.sub(r"\D+", "", str(value or ""))
        if not digits:
            return False
        if len(digits) == 10:
            return False
        return not (len(digits) == 11 and digits.startswith("1"))

    def _append_review_note(self, extraction: dict[str, Any], note: str) -> None:
        """Purpose: keep review notes unique while preserving model notes."""

        notes = extraction.setdefault("review_notes", [])
        if isinstance(notes, list) and note not in notes:
            notes.append(note)

    def _item_context(self, item: dict[str, Any]) -> dict[str, Any]:
        """Purpose: keep Milwaukee prompt context consistent across LLM calls."""

        return {
            "event_id": item["event_id"],
            "meeting_date": item["meeting_date"],
            "matter_id": item["matter_id"],
            "city_file": item.get("city_file"),
            "title": item["title"],
        }

    def _payload(
        self,
        events: list[EventRecord],
        items: list[dict[str, Any]],
        *,
        include_text: bool,
        include_llm: bool,
        projects: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Purpose: shape one serializable MVP response."""

        contact_evidence_rows = self._contact_evidence_rows(projects or [])
        public_projects = [self._public_project(project) for project in projects or []]
        return {
            "source": "milwaukee_cpc_mvp",
            "body_name": BODY_NAME,
            "include_text": include_text,
            "include_llm": include_llm,
            "event_count": len(events),
            "events": [
                {
                    "event_id": event.event_id,
                    "meeting_date": event.meeting_date.isoformat(),
                    "agenda_url": event.agenda_url,
                    "detail_url": event.detail_url,
                }
                for event in events
            ],
            "item_count": len(items),
            "project_count": len(public_projects),
            "tracking_summary": self._tracking_summary(items, public_projects, contact_evidence_rows),
            "contact_evidence_rows": contact_evidence_rows,
            "projects": public_projects,
            "items": [self._public_item(item) for item in items],
        }

    def _tracking_summary(
        self,
        items: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        contact_evidence_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Purpose: expose scrape counters for Milwaukee pattern analysis."""

        ranked_docs = [doc for item in items for doc in item.get("evidence_documents") or []]
        extracted_docs = [doc for doc in ranked_docs if doc.get("content_hash") or doc.get("error")]
        contact_docs = [doc for project in projects for doc in project.get("contact_dig_documents") or []]
        contacts = [contact for project in projects for contact in project.get("contacts") or [] if isinstance(contact, dict)]
        primary_contact_projects = [
            project
            for project in projects
            if (project.get("contact_search_verification") or {}).get("status") == "primary_contact_found"
        ]
        project_team_only_projects = [
            project
            for project in projects
            if (project.get("contact_search_verification") or {}).get("status") == "project_team_contact_only"
        ]
        no_contact_projects = [
            project
            for project in projects
            if (project.get("contact_search_verification") or {}).get("status") == "no_direct_contact_found"
        ]
        enrichment_candidates = [
            row
            for project in projects
            for row in project.get("external_enrichment_candidates") or []
        ]
        return {
            "items": len(items),
            "ranked_docs": len(ranked_docs),
            "selected_initial_docs": sum(1 for doc in ranked_docs if doc.get("selected")),
            "extracted_docs": len(extracted_docs),
            "contact_dig_docs": len(contact_docs),
            "contact_evidence_rows": len(contact_evidence_rows),
            "contacts": len(contacts),
            "useful_contacts": sum(1 for contact in contacts if contact.get("useful_for_outreach") is True),
            "primary_contact_projects": len(primary_contact_projects),
            "project_team_only_projects": len(project_team_only_projects),
            "verified_no_contact_projects": len(no_contact_projects),
            "external_enrichment_candidates": len(enrichment_candidates),
            "drawing_docs": sum(1 for doc in ranked_docs if self._is_drawing_document(doc.get("name"))),
            "document_families": sorted(
                {
                    self._contact_document_family(doc.get("name"))
                    for doc in [*ranked_docs, *contact_docs]
                    if doc.get("name")
                }
            ),
        }

    def _contact_evidence_rows(self, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Purpose: flatten contact evidence so operator QA can feed pattern learning."""

        rows: list[dict[str, Any]] = []
        for project in projects:
            documents = self._project_tracking_documents(project)
            for contact in project.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                snippet = str(contact.get("evidence_snippet") or "").strip()
                source = self._match_contact_source(snippet, documents)
                rows.append(
                    {
                        "project_key": project.get("project_key"),
                        "project_name": project.get("project_name"),
                        "project_address": project.get("project_address"),
                        "unit_count": project.get("unit_count"),
                        "related_files": [row.get("city_file") for row in project.get("related_files") or []],
                        "city_file": source.get("city_file"),
                        "matter_id": source.get("matter_id"),
                        "document_name": source.get("name"),
                        "document_family": self._contact_document_family(source.get("name")),
                        "attachment_id": source.get("attachment_id"),
                        "source_url": source.get("source_url"),
                        "content_hash": source.get("content_hash"),
                        "docling_mode": source.get("docling_mode"),
                        "text_chars": source.get("text_chars"),
                        "text_offset_start": source.get("offset_start"),
                        "text_offset_end": source.get("offset_end"),
                        "pdf_page": None,
                        "evidence_snippet": snippet or None,
                        "contact_role": contact.get("role"),
                        "company": contact.get("company"),
                        "name": contact.get("name"),
                        "phone": contact.get("phone"),
                        "email": contact.get("email"),
                        "mailing_address": contact.get("mailing_address"),
                        "outreach_priority": contact.get("outreach_priority"),
                        "useful_for_outreach": contact.get("useful_for_outreach"),
                        "review_flags": contact.get("review_flags"),
                    }
                )
        return rows

    def _project_tracking_documents(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """Purpose: collect extracted project documents with enough metadata to match snippets."""

        rows: list[dict[str, Any]] = []
        for item in project.get("items") or []:
            for document in item.get("evidence_documents") or []:
                if document.get("content_hash") or document.get("error"):
                    rows.append(document | self._tracking_item_fields(item))
        for document in project.get("contact_dig_documents") or []:
            rows.append(document)
        return rows

    def _tracking_item_fields(self, item: dict[str, Any]) -> dict[str, Any]:
        """Purpose: copy item identifiers into flattened tracking rows."""

        return {
            "city_file": item.get("city_file"),
            "matter_id": item.get("matter_id"),
            "event_id": item.get("event_id"),
            "meeting_date": item.get("meeting_date"),
        }

    def _match_contact_source(self, snippet: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
        """Purpose: locate the document metadata that produced a contact snippet."""

        if not documents:
            return {}
        if not snippet:
            return documents[0] | {"offset_start": None, "offset_end": None}
        snippet_key = self._snippet_key(snippet)
        for document in documents:
            text = str(document.get("_tracking_text") or "")
            offset = self._tracking_text_offset(text, snippet)
            if offset is not None:
                start, end = offset
                return document | {"offset_start": start, "offset_end": end}
            if snippet_key and snippet_key in self._snippet_key(text):
                return document | {"offset_start": None, "offset_end": None}
        return documents[0] | {"offset_start": None, "offset_end": None}

    def _tracking_text_offset(self, text: str, snippet: str) -> tuple[int, int] | None:
        """Purpose: find exact or whitespace-normalized snippet offsets when available."""

        if not text or not snippet:
            return None
        exact = text.find(snippet)
        if exact >= 0:
            return exact, exact + len(snippet)
        compact_text = re.sub(r"\s+", " ", text)
        compact_snippet = re.sub(r"\s+", " ", snippet).strip()
        compact = compact_text.find(compact_snippet)
        if compact >= 0:
            return compact, compact + len(compact_snippet)
        return None

    def _snippet_key(self, value: Any) -> str:
        """Purpose: compare evidence snippets despite OCR spacing and punctuation."""

        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def _public_project(self, project: dict[str, Any]) -> dict[str, Any]:
        """Purpose: remove internal text buffers from grouped project output."""

        public = {
            key: value
            for key, value in project.items()
            if key not in {"items", "representative_item", "representative_candidate"}
        }
        if "contact_dig_documents" in public:
            public["contact_dig_documents"] = [self._public_document(document) for document in public.get("contact_dig_documents") or []]
        return public | {"items": [self._public_item(item) for item in project.get("items", [])]}

    def _public_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Purpose: avoid returning full source text in browser snapshots."""

        public = {key: value for key, value in item.items() if not key.startswith("_")}
        if "evidence_documents" in public:
            public["evidence_documents"] = [self._public_document(document) for document in public.get("evidence_documents") or []]
        return public

    def _public_document(self, document: dict[str, Any]) -> dict[str, Any]:
        """Purpose: hide internal tracking text while keeping source metadata."""

        return {key: value for key, value in document.items() if not str(key).startswith("_")}

    def _item_payload(self, item: MilwaukeeCandidateItem) -> dict[str, Any]:
        """Purpose: serialize a candidate item for the MVP UI."""

        return {
            "event_id": item.event_id,
            "meeting_date": item.meeting_date.isoformat(),
            "agenda_sequence": item.agenda_sequence,
            "matter_id": item.matter_id,
            "city_file": item.city_file,
            "title": item.title,
            "candidate_score": item.candidate_score,
            "candidate_reason": item.candidate_reason,
            "evidence_documents": [asdict(document) for document in item.evidence_documents],
        }

    def _candidate_sort_key(self, item: MilwaukeeCandidateItem) -> tuple[int, int, str, str]:
        """Purpose: order likely targets before weaker review candidates."""

        doc_score = max((document.score for document in item.evidence_documents), default=0)
        return item.candidate_score, doc_score, item.meeting_date.isoformat(), item.matter_id

    def _project_sort_key(self, project: dict[str, Any]) -> tuple[int, float, str]:
        """Purpose: put target project groups first in the MVP output."""

        representative = project.get("representative_item") or {}
        return (
            1 if project.get("target_project") is True else 0,
            float(project.get("confidence") or 0),
            str(representative.get("city_file") or ""),
        )

    def _identity_token(self, value: Any) -> str:
        """Purpose: normalize project identity fields for same-building grouping."""

        text = str(value or "").lower()
        text = re.sub(r"\bmilwaukee\b|\bwi\b|\bwisconsin\b|\b532\d{2}\b", " ", text)
        text = re.sub(r"\bs\.?\b", " south ", text)
        text = re.sub(r"\bn\.?\b", " north ", text)
        text = re.sub(r"\be\.?\b", " east ", text)
        text = re.sub(r"\bw\.?\b", " west ", text)
        text = re.sub(r"\bst\.?\b", " street ", text)
        text = re.sub(r"\bave\.?\b", " avenue ", text)
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    def _item_title(self, raw: dict[str, Any]) -> str:
        """Purpose: combine Legistar title fields without duplicating text."""

        parts = [
            self._text_or_none(raw.get("EventItemTitle")),
            self._text_or_none(raw.get("EventItemMatterName")),
            self._text_or_none(raw.get("EventItemAgendaNote")),
        ]
        seen: set[str] = set()
        cleaned: list[str] = []
        for part in parts:
            if not part or part in seen:
                continue
            seen.add(part)
            cleaned.append(part)
        return " ".join(cleaned)

    def _text_or_none(self, value: Any) -> str | None:
        """Purpose: normalize optional Legistar text fields."""

        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text or None

    def _safe_name(self, value: Any) -> str:
        """Purpose: keep temporary filenames portable."""

        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "source")).strip("._") or "source"
