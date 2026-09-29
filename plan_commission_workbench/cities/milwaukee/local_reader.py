"""Milwaukee evidence reading without hosted model calls."""

from __future__ import annotations

import logging
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlparse

from ...contact_evidence import ContactEvidenceSelector
from ...document_evidence import DocumentEvidenceReader
from ...local_document_store import LocalDocumentStore

LOG = logging.getLogger(__name__)
EXTRACTION_VERSION = "local-contacts-v1"


class LocalDocumentProcessor:
    """Purpose: download, read, select, and cache compact source evidence."""

    def __init__(self, legistar, store=None, reader=None, selector=None, tmp_root=None) -> None:
        self.legistar = legistar
        self.store = store
        self.reader = reader or DocumentEvidenceReader()
        self.selector = selector or ContactEvidenceSelector()
        self.tmp_root = tmp_root

    def process(self, document: dict[str, Any], *, use_ocr=True, refresh_sources=False) -> dict[str, Any]:
        """Purpose: reuse a known source or fail visibly while reading it."""

        url = document["source_url"]
        version = f"{EXTRACTION_VERSION}:ocr={use_ocr}"
        try:
            saved = self.store.get(url, version) if self.store and not refresh_sources else None
            if saved is not None:
                return self._attach_source(document, saved, cache_hit=True)
            payload = self._read(url, use_ocr)
            if self.store:
                self.store.save(url, version, payload)
            return self._attach_source(document, payload, cache_hit=False)
        except Exception:
            LOG.exception("Milwaukee local document reading failed; stopping: %s", url)
            raise

    def _read(self, url: str, use_ocr: bool) -> dict[str, Any]:
        """Purpose: clean temporary PDFs even if local reading fails."""

        parent = self.tmp_root if self.tmp_root and self.tmp_root.exists() else None
        with tempfile.TemporaryDirectory(prefix="pcw_local_document_", dir=parent) as temporary:
            downloaded = self.legistar.download_file(url, Path(temporary) / "source.pdf")
            evidence = self.reader.read(downloaded.path, use_ocr=use_ocr)
            selected = self.selector.extract(evidence)
        return {
            "content_hash": downloaded.content_hash,
            "text_chars": evidence["text_chars"],
            "coverage": self._coverage(evidence),
            "contacts": selected["contacts"],
            "unresolved_blocks": selected["unresolved_blocks"],
            "warnings": list(dict.fromkeys([*evidence.get("warnings", []), *selected.get("warnings", [])])),
            "llm_calls": 0,
        }

    def _coverage(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Purpose: retain page coverage without storing entire document text."""

        pages = evidence["pages"]
        return {
            "page_count": evidence["page_count"],
            "pages_read": len(pages),
            "pages_ocr": sum(any(block["method"] == "ocr" for block in page["blocks"]) for page in pages),
            "unread_pages": [page["page"] for page in pages if page.get("issues")],
            "coverage_complete": evidence["coverage_complete"],
            "pages": [{key: page[key] for key in ("page", "coverage", "issues")} for page in pages],
        }

    def _attach_source(self, document, payload, *, cache_hit):
        """Purpose: bind each contact to its actual attachment and page."""

        source = {"source_url": document["source_url"], "document_name": document["name"], "content_hash": payload["content_hash"]}
        contacts = [contact | source for contact in payload["contacts"]]
        return document | payload | {"contacts": contacts, "cache_hit": cache_hit}


class MilwaukeeLocalReader:
    """Purpose: discover candidate files and read their contact evidence locally."""

    def __init__(self, discovery, processor=None) -> None:
        self.discovery = discovery
        self.processor = processor or self._processor()

    def _processor(self) -> LocalDocumentProcessor:
        """Purpose: keep source evidence local to the configured workbench data root."""

        root = self.discovery.tmp_root
        store = LocalDocumentStore(root.parent / "local_document_evidence.sqlite3") if root else None
        return LocalDocumentProcessor(self.discovery.legistar, store=store, tmp_root=root)

    def run(self, *, date_from, date_to, max_items=6, documents_per_item=5, use_ocr=True, refresh_sources=False):
        """Purpose: return candidates with page-linked contacts, making zero LLM calls."""

        if date_from > date_to:
            raise ValueError("Start date must not be after end date")
        events = self.discovery.legistar.list_body_events("CITY PLAN COMMISSION", date_from, date_to)
        items = self._discover(events)
        candidates = items[:max(1, min(max_items, 25))]
        projects = [self._read_item(item, documents_per_item, use_ocr, refresh_sources) for item in candidates]
        return self._payload(events, candidates, projects, len(items))

    def _discover(self, events):
        """Purpose: preserve candidate uncertainty while making every PDF eligible."""

        items = []
        seen = set()
        for event in events:
            for raw in self.discovery.legistar.fetch_event_items(event.event_id):
                matter_id = str(raw.get("EventItemMatterId") or "")
                if not matter_id or matter_id in seen:
                    continue
                title = self.discovery._item_title(raw)
                score, reason = self.discovery._candidate_score(title, 120)
                if score < 20:
                    continue
                seen.add(matter_id)
                items.append({
                    "matter_id": matter_id, "event_id": event.event_id,
                    "meeting_date": event.meeting_date.isoformat(), "title": title,
                    "city_file": raw.get("EventItemMatterFile"), "candidate_score": score,
                    "candidate_reason": reason, "evidence_documents": self._documents(raw),
                    "skipped_attachments": self._skipped_attachments(raw),
                })
        return sorted(items, key=lambda item: (item["candidate_score"], item["meeting_date"]), reverse=True)

    def _documents(self, raw):
        """Purpose: prioritize plans and title sheets without excluding other PDFs."""

        documents = []
        seen = set()
        for attachment in raw.get("EventItemMatterAttachments") or []:
            name = str(attachment.get("MatterAttachmentName") or "Attachment")
            url = self.discovery._attachment_url(raw, attachment)
            if not url or url in seen or not self._is_pdf(name, url):
                continue
            seen.add(url)
            documents.append({"name": name, "source_url": url, "attachment_id": str(attachment.get("MatterAttachmentId") or "")})
        return sorted(documents, key=lambda doc: self._document_priority(doc["name"]), reverse=True)

    def _is_pdf(self, name, url):
        """Purpose: accept both direct PDFs and Legistar attachment-file endpoints."""

        parsed = urlparse(url)
        path = parsed.path.lower()
        unsupported = (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".jpg", ".jpeg", ".png", ".tif")
        if path.endswith(unsupported) or name.lower().endswith(unsupported):
            return False
        query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
        viewer = path.endswith("/view.ashx") and any(value.lower() in {"f", "a"} for value in query.get("m", []))
        return path.endswith((".pdf", "/file")) or name.lower().endswith(".pdf") or viewer

    def _skipped_attachments(self, raw):
        """Purpose: keep unsupported evidence visible instead of claiming full coverage."""

        skipped = []
        for attachment in raw.get("EventItemMatterAttachments") or []:
            name = str(attachment.get("MatterAttachmentName") or "Attachment")
            url = self.discovery._attachment_url(raw, attachment)
            if not url or not self._is_pdf(name, url):
                skipped.append({"name": name, "source_url": url, "reason": "Unsupported attachment type or missing PDF link"})
        return skipped

    def _document_priority(self, name):
        """Purpose: favor likely contact blocks while retaining other evidence."""

        priorities = ((r"title|project team|exhibit a continued", 150),
                      (r"drawings?|plans?|narrative", 120), (r"staff report", 100),
                      (r"affidavit|exhibit|application", 80), (r"matrix|resolution", 50))
        return max((score for pattern, score in priorities if re.search(pattern, name, re.I)), default=10)

    def _read_item(self, item, limit, use_ocr, refresh_sources):
        """Purpose: report file-level evidence without claiming LLM project triage."""

        available = item["evidence_documents"]
        documents = [self.processor.process(doc, use_ocr=use_ocr, refresh_sources=refresh_sources)
                     for doc in available[:max(1, min(limit, 25))]]
        contacts = [contact for doc in documents for contact in doc["contacts"]]
        selected_all = len(documents) == len(available)
        skipped = item.get("skipped_attachments", [])
        complete = bool(documents) and selected_all and not skipped and all(doc["coverage"]["coverage_complete"] for doc in documents)
        notes = self._review_notes(documents, len(available), complete)
        if skipped:
            notes.append("Unsupported attachments remain unread: " + ", ".join(row["name"] for row in skipped))
        return {
            "project_key": f"milwaukee:matter:{item['matter_id']}", "city_file": item["city_file"],
            "candidate_title": item["title"], "project_name": f"CPC file {item['city_file'] or item['matter_id']}",
            "target_project": None, "coverage_complete": complete, "document_selection_complete": selected_all,
            "available_document_count": len(available), "evidence_documents": documents,
            "skipped_attachments": skipped,
            "contacts": contacts, "extraction": {"contacts": contacts, "review_notes": notes},
            "unresolved_blocks": [block for doc in documents for block in doc["unresolved_blocks"]],
            "related_files": [{"city_file": item["city_file"], "matter_id": item["matter_id"], "title": item["title"]}],
        }

    def _review_notes(self, documents, total, complete):
        """Purpose: distinguish missed coverage from an absence of contact evidence."""

        notes = ["Local evidence candidates require review; printed owner roles are not inferred to be developers."]
        if len(documents) < total:
            notes.append(f"Document limit: read {len(documents)} of {total} available PDFs.")
        if not complete:
            notes.append("Search coverage is incomplete. Missing contacts are unresolved, not verified absent.")
        return notes + list(dict.fromkeys(warning for doc in documents for warning in doc["warnings"]))

    def _payload(self, events, items, projects, candidate_total):
        """Purpose: expose local processing evidence and coverage in the existing UI."""

        documents = [doc for project in projects for doc in project["evidence_documents"]]
        contacts = [contact for project in projects for contact in project["contacts"]]
        complete = bool(projects) and len(items) == candidate_total and all(project["coverage_complete"] for project in projects)
        return {
            "source": "milwaukee_cpc_mvp", "body_name": "CITY PLAN COMMISSION", "processing_mode": "local",
            "llm_calls": 0, "event_count": len(events), "item_count": len(items), "project_count": len(projects),
            "events": [{"event_id": event.event_id, "meeting_date": event.meeting_date.isoformat(),
                        "agenda_url": event.agenda_url, "detail_url": event.detail_url} for event in events],
            "items": items, "projects": projects, "contact_evidence_rows": contacts, "coverage_complete": complete,
            "warnings": [] if complete else ["The selected search is incomplete; absent fields are not proof that no contact exists."],
            "tracking_summary": {"items": len(items), "candidate_files_available": candidate_total,
                                 "extracted_docs": len(documents), "contact_evidence_rows": len(contacts),
                                 "contacts": len(contacts), "cache_hits": sum(doc["cache_hit"] for doc in documents),
                                 "pages_read": sum(doc["coverage"]["pages_read"] for doc in documents), "llm_calls": 0},
        }
