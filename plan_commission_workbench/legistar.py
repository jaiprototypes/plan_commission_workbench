"""Madison Legistar API client."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import DownloadError
from .models import AttachmentRecord, DownloadedFile, EventRecord

LOG = logging.getLogger(__name__)
BASE_URL = "https://webapi.legistar.com/v1/{tenant}"
TIMEOUT_SECONDS = 30

EXCLUDE_ATTACHMENT_KEYS = (
    "letter of intent",
    "locator",
    "map",
    "plans",
    "site plan",
    "public comment",
    "staff comment",
    "demolition",
    "floor plan",
    "management plan",
)

ATTACHMENT_SCORES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"land\s*use.*application", re.I), 100),
    (re.compile(r"zoning.*application", re.I), 80),
    (re.compile(r"(conditional\s*use|cup).*application", re.I), 70),
    (re.compile(r"(pud|gdp|pip).*application", re.I), 60),
    (re.compile(r"subdivision.*application", re.I), 50),
    (re.compile(r"application", re.I), 20),
)


class LegistarClient:
    """Purpose: fetch Madison events, agenda PDFs, matters, and attachments."""

    def __init__(self, tenant: str = "madison", session: requests.Session | None = None) -> None:
        self.tenant = tenant
        self.base_url = BASE_URL.format(tenant=tenant)
        self.session = session or requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def list_plan_commission_events(self, date_from: dt.date, date_to: dt.date) -> list[EventRecord]:
        """Purpose: fetch Madison Plan Commission events inside inclusive dates."""

        params = {
            "$filter": (
                "(EventBodyName eq 'PLAN COMMISSION')"
                f" and (EventDate ge datetime'{date_from.isoformat()}T00:00:00')"
                f" and (EventDate le datetime'{date_to.isoformat()}T23:59:59')"
            ),
            "$orderby": "EventDate asc",
        }
        payload = self._get_json(f"{self.base_url}/events", params=params)
        events: list[EventRecord] = []
        for raw in payload if isinstance(payload, list) else []:
            event = self._event_from_json(raw)
            if event:
                events.append(event)
        return events

    def fetch_event_items(self, event_id: str | int) -> list[dict[str, Any]]:
        """Purpose: fetch agenda items with attachment metadata."""

        url = f"{self.base_url}/Events/{event_id}"
        payload = self._get_json(url, params={"EventItems": "1", "EventItemAttachments": "1"})
        return list(payload.get("EventItems") or []) if isinstance(payload, dict) else []

    def find_application_attachment(self, agenda_item: dict[str, Any], event_items: list[dict[str, Any]]) -> AttachmentRecord | None:
        """Purpose: select the best standardized application PDF for one agenda hit."""

        matched = self._match_event_item(agenda_item, event_items)
        if not matched:
            return None
        best = self._best_application_attachment(matched.get("EventItemMatterAttachments") or [])
        if not best:
            return None
        matter_id = str(matched.get("EventItemMatterId") or agenda_item.get("city_item_id") or "").strip()
        attachment_id = str(best.get("MatterAttachmentId") or "").strip()
        if not matter_id or not attachment_id:
            return None
        return AttachmentRecord(
            agenda_item_id=int(agenda_item["id"]),
            city_item_id=str(agenda_item.get("city_item_id") or matter_id),
            file_id=str(agenda_item.get("file_id") or matched.get("EventItemMatterFile") or "") or None,
            attachment_id=attachment_id,
            source_url=f"{self.base_url}/Matters/{matter_id}/Attachments/{attachment_id}/File",
            name=str(best.get("MatterAttachmentName") or "Land Use Application.pdf"),
        )

    def download_file(self, url: str, destination: Path) -> DownloadedFile:
        """Purpose: download a source PDF to temp storage and hash it."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        try:
            with self.session.get(url, stream=True, timeout=TIMEOUT_SECONDS) as response:
                response.raise_for_status()
                with destination.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        digest.update(chunk)
                        fh.write(chunk)
        except Exception as exc:
            raise DownloadError(f"Failed to download {url}: {exc}") from exc
        return DownloadedFile(path=destination, content_hash=digest.hexdigest())

    def _event_from_json(self, raw: dict[str, Any]) -> EventRecord | None:
        """Purpose: normalize one Legistar event payload."""

        event_id = raw.get("EventId")
        raw_date = str(raw.get("EventDate") or "").split("T")[0]
        agenda_url = self._find_pdf_url(raw.get("EventAgendaFile"), raw.get("EventInSiteURL"))
        if not event_id or not raw_date or not agenda_url:
            return None
        try:
            meeting_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            return None
        return EventRecord(
            event_id=str(event_id),
            meeting_date=meeting_date,
            agenda_url=agenda_url,
            detail_url=raw.get("EventInSiteURL"),
            agenda_status=raw.get("EventAgendaStatusName"),
            raw=raw,
        )

    def _find_pdf_url(self, direct: str | None, detail_url: str | None) -> str | None:
        """Purpose: resolve direct or linked Legistar PDF URLs."""

        if direct and (direct.lower().endswith(".pdf") or "view.ashx" in direct.lower()):
            return direct
        if not detail_url:
            return None
        try:
            response = self.session.get(detail_url, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
        except Exception:
            return None
        for href in re.findall(r"href=[\"']([^\"']+)[\"']", response.text, flags=re.I):
            lower = href.lower()
            if lower.endswith(".pdf") or "view.ashx?m=f" in lower:
                return urljoin(detail_url, href)
        return None

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        """Purpose: GET JSON with a consistent timeout."""

        response = self.session.get(url, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def _match_event_item(self, agenda_item: dict[str, Any], event_items: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Purpose: match stored agenda IDs to Legistar event-item metadata."""

        wanted = {
            str(agenda_item.get("city_item_id") or "").strip(),
            str(agenda_item.get("file_id") or "").strip(),
        } - {""}
        for item in event_items:
            keys = {
                str(item.get("EventItemMatterId") or "").strip(),
                str(item.get("EventItemMatterFile") or "").strip(),
                str(item.get("EventItemAgendaSequence") or "").strip(),
            } - {""}
            if wanted & keys:
                return item
        return None

    def _best_application_attachment(self, attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Purpose: choose the most application-like attachment and ignore noise."""

        best: tuple[int, dict[str, Any]] | None = None
        for attachment in attachments:
            name = str(attachment.get("MatterAttachmentName") or "")
            lower = name.lower()
            if any(key in lower for key in EXCLUDE_ATTACHMENT_KEYS):
                continue
            score = max((value for pattern, value in ATTACHMENT_SCORES if pattern.search(name)), default=0)
            if score and (best is None or score > best[0]):
                best = (score, attachment)
        return best[1] if best else None
