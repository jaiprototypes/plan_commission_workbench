"""Agenda segmentation and application section clipping."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .models import AgendaSegment

ITEM_RE = re.compile(r"^\s*(?P<number>\d{1,3})[\.)]\s+(?P<ref>\d{4,})\s+(?P<desc>.+)$")
NON_ITEM_TAIL_RE = re.compile(
    r"\s*(?:#+\s*)?(?:Secretary's Report|Member Announcements(?:,\s*Communications or Business Items)?|Adjournment|Registrations)\b.*$",
    re.IGNORECASE,
)
NON_ACTION_ITEM_RE = re.compile(r"^\s*(?:Plan Commission\s+)?Public Comment Period\b", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")


def has_non_item_agenda_tail(text: str) -> bool:
    """Purpose: detect agenda boilerplate accidentally merged into an item."""

    return bool(NON_ITEM_TAIL_RE.search(text))


def is_non_action_agenda_item(text: str) -> bool:
    """Purpose: identify agenda rows that are not project applications."""

    return bool(NON_ACTION_ITEM_RE.search(SPACE_RE.sub(" ", text).strip()))


class AgendaSegmenter:
    """Purpose: turn Docling agenda text into candidate Legistar line items."""

    def segment(
        self,
        text: str,
        *,
        event_id: str,
        meeting_date: dt.date,
        event_items: list[dict[str, Any]],
    ) -> list[AgendaSegment]:
        """Purpose: segment locally, then enrich IDs from Legistar event items."""

        by_ref = self._event_item_lookup(event_items)
        found = self._segments_from_text(text, event_id, meeting_date, by_ref)
        # Event item metadata fills gaps when the agenda text omits wrapping details.
        return self._merge_event_items(found, event_id, meeting_date, event_items)

    def _segments_from_text(
        self,
        text: str,
        event_id: str,
        meeting_date: dt.date,
        by_ref: dict[str, dict[str, Any]],
    ) -> list[AgendaSegment]:
        """Purpose: parse numbered agenda lines and wrapped continuation text."""

        segments: list[AgendaSegment] = []
        current_ref: str | None = None
        current_desc: list[str] = []
        for line in text.splitlines():
            match = ITEM_RE.match(line.strip())
            if match:
                self._append_segment(segments, event_id, meeting_date, current_ref, current_desc, by_ref)
                current_ref = match.group("ref")
                current_desc = [match.group("desc")]
                continue
            if current_ref and line.strip():
                current_desc.append(line.strip())
        self._append_segment(segments, event_id, meeting_date, current_ref, current_desc, by_ref)
        return segments

    def _append_segment(
        self,
        segments: list[AgendaSegment],
        event_id: str,
        meeting_date: dt.date,
        ref: str | None,
        desc_parts: list[str],
        by_ref: dict[str, dict[str, Any]],
    ) -> None:
        """Purpose: create one segment once all wrapped lines are known."""

        if not ref:
            return
        event_item = by_ref.get(ref, {})
        city_item_id = str(event_item.get("EventItemMatterId") or ref).strip()
        file_id = str(event_item.get("EventItemMatterFile") or ref).strip() or None
        raw_description = " ".join(desc_parts) or event_item.get("EventItemMatterName") or ""
        description = self._clean(self._trim_non_item_tail(raw_description))
        if description:
            segments.append(AgendaSegment(event_id, city_item_id, file_id, meeting_date, description))

    def _merge_event_items(
        self,
        segments: list[AgendaSegment],
        event_id: str,
        meeting_date: dt.date,
        event_items: list[dict[str, Any]],
    ) -> list[AgendaSegment]:
        """Purpose: preserve Legistar IDs for items not visible in extracted text."""

        by_city = {segment.city_item_id: segment for segment in segments}
        for item in event_items:
            city_item_id = str(item.get("EventItemMatterId") or "").strip()
            if not city_item_id or city_item_id in by_city:
                continue
            description = self._clean(str(item.get("EventItemMatterName") or ""))
            if not description:
                continue
            by_city[city_item_id] = AgendaSegment(
                event_id=event_id,
                city_item_id=city_item_id,
                file_id=str(item.get("EventItemMatterFile") or "") or None,
                meeting_date=meeting_date,
                description=description,
            )
        return list(by_city.values())

    def _event_item_lookup(self, event_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Purpose: map both visible and internal Legistar IDs."""

        lookup: dict[str, dict[str, Any]] = {}
        for item in event_items:
            for key in ("EventItemMatterId", "EventItemMatterFile", "EventItemAgendaSequence"):
                value = str(item.get(key) or "").strip()
                if value:
                    lookup[value] = item
        return lookup

    def _clean(self, text: str) -> str:
        """Purpose: normalize OCR/Docling whitespace."""

        return SPACE_RE.sub(" ", text).strip()

    def _trim_non_item_tail(self, text: str) -> str:
        """Purpose: stop final agenda items before staff report boilerplate."""

        return NON_ITEM_TAIL_RE.sub("", text)


class SectionClipper:
    """Purpose: limit application LLM input to standardized Sections 3 and 5."""

    def clip_sections_3_and_5(self, text: str) -> str:
        """Purpose: return only Section 3 and Section 5 text."""

        section3 = self._section(text, "3", {"4", "5"})
        section5 = self._section(text, "5", {"6", "7"})
        return "\n\n".join(part for part in (section3, section5) if part.strip())

    def _section(self, text: str, number: str, end_numbers: set[str]) -> str:
        """Purpose: extract one numbered section using heading boundaries."""

        starts = list(self._heading_pattern(number).finditer(text))
        if not starts:
            return ""
        start = starts[0].start()
        end = len(text)
        for end_number in end_numbers:
            match = self._heading_pattern(end_number).search(text[start + 1 :])
            if match:
                end = min(end, start + 1 + match.start())
        return text[start:end].strip()

    def _heading_pattern(self, number: str) -> re.Pattern[str]:
        """Purpose: match Docling Markdown bullets and compressed form headings."""

        return re.compile(
            rf"(?im)^\s*(?:[#>*\-•·\[\]\sxX口日]+)?(?:section\s*)?{re.escape(number)}"
            r"(?![A-Za-z0-9])\s*[\.:)\-]?\s*.+$"
        )
