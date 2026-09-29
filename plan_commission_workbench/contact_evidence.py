"""Select grounded contact evidence from locally read PDF coordinates.

This deliberately small vocabulary resolves labelled contact panels. Unlabelled
details remain evidence for review rather than inferred developer contacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_ROLES = {
    "OWNER": "owner", "PROPERTY OWNER": "owner", "BUILDING OWNER": "owner",
    "DEVELOPER": "developer", "OWNER/DEVELOPER": "developer",
    "OWNER / DEVELOPER": "developer", "APPLICANT": "applicant",
    "CONTRACTOR": "contractor", "GENERAL CONTRACTOR": "contractor",
    "ARCHITECT": "architect", "PROJECT ARCHITECT": "architect",
    "ENGINEER": "engineer", "STRUCTURAL ENGINEER": "engineer",
    "CIVIL ENGINEER": "engineer", "LANDSCAPE ARCHITECT": "architect",
}
_EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}")
_WEB = re.compile(r"(?<![\w@.-])(?:https?://|www\.)?[a-z0-9][a-z0-9.-]*\.(?:com|org|net|us|biz|co|io)(?:/[^\s]*)?\b", re.I)
# Keep every printed digit, including malformed overlong final groups.
_PHONE = re.compile(r"(?<![\w\d])(?:\+?1[ .-])?(?:\(\d{3}\)\s*|\d{3}[ .-])\d{3}[ .-]\d{4,}(?:\s*(?:ext\.?|x)\s*\d+)?(?!\d)", re.I)
_LABELLED_PHONE = re.compile(r"^(?:P|PH|PHONE|TELEPHONE|TEL|MOBILE|CELL)\s*[:.]\s*([+()\d][()\d ./-]*(?:\s*(?:ext\.?|x)\s*\d+)?)\s*$", re.I)
_STREET = re.compile(r"^(?:\d+\s+[A-Za-z]|[NSEW]\d+\s+[NSEW]?\d+\b|P\.?\s*O\.?\s+BOX\b)", re.I)
_CITY = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", re.I)
_SUITE = re.compile(r"^(?:SUITE|STE\.?|FLOOR|FL\.?)\s+\w+\b", re.I)
_PERSON = re.compile(r"(?:,\s*(?:AIA|PE|P\.E\.|ASLA|RA)\b|^(?:ATTN|CONTACT|ATTENTION)\s*:)", re.I)
_STAFF = re.compile(r"\b(?:CITY OF MILWAUKEE|DEPARTMENT OF CITY DEVELOPMENT|CITY PLAN COMMISSION|CITY STAFF|PLANNING STAFF)\b", re.I)


@dataclass(frozen=True)
class _Text:
    """A bounded source fragment; ids retain exact input provenance."""

    text: str
    bbox: tuple[float, float, float, float]
    method: str
    ids: frozenset[int]
    ocr_confidence: float | None = None

    @property
    def height(self) -> float:
        """Use a positive line scale for geometry thresholds."""
        return max(1.0, self.bbox[3] - self.bbox[1])

    @property
    def center_y(self) -> float:
        """Return the baseline-independent row center."""
        return (self.bbox[1] + self.bbox[3]) / 2


def _union(parts: list[_Text]) -> tuple[float, float, float, float]:
    """Enclose only the evidence actually selected."""
    return (min(p.bbox[0] for p in parts), min(p.bbox[1] for p in parts),
            max(p.bbox[2] for p in parts), max(p.bbox[3] for p in parts))


def _merge(parts: list[_Text], separator: str = " ") -> _Text:
    """Join nearby fragments without losing source ids or OCR provenance."""
    methods = {p.method for p in parts}
    scores = [p.ocr_confidence for p in parts if p.ocr_confidence is not None]
    return _Text(separator.join(p.text for p in parts), _union(parts),
                 methods.pop() if len(methods) == 1 else "mixed",
                 frozenset().union(*(p.ids for p in parts)), min(scores) if scores else None)


class ContactEvidenceSelector:
    """Extract conservative contact panels without a model or external calls."""

    def extract(self, document: dict) -> dict:
        """Return typed contacts, unresolved evidence, and coverage warnings."""
        contacts, unresolved = [], []
        warnings = list(document.get("warnings") or [])
        for page in document.get("pages", []):
            found, pending = self._page(page)
            contacts.extend(found)
            unresolved.extend(pending)
        if not document.get("coverage_complete", False):
            warnings.append("incomplete_document_coverage")
        return {"contacts": contacts, "unresolved_blocks": unresolved,
                "warnings": list(dict.fromkeys(warnings))}

    def _fragments(self, page: dict) -> list[_Text]:
        """Normalize blocks, retaining original line breaks as separate rows."""
        result = []
        for block in page.get("blocks", []):
            text = str(block.get("text") or "").strip()
            box = block.get("bbox")
            if not text or not box or len(box) != 4:
                continue
            left, bottom, right, top = map(float, box)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            step = max(1.0, top - bottom) / len(lines)
            for line_index, line in enumerate(lines):
                line_box = (left, top - (line_index + 1) * step, right, top - line_index * step)
                score = block.get("confidence") if block.get("method") == "ocr" else None
                result.append(_Text(line, line_box, block.get("method", "text_layer"),
                                    frozenset({len(result)}), score))
        return result

    def _lines(self, fragments: list[_Text]) -> list[_Text]:
        """Join same-row fragments only over short, ordinary word spaces."""
        rows: list[list[_Text]] = []
        for fragment in sorted(fragments, key=lambda p: (-p.center_y, p.bbox[0])):
            row = next((r for r in rows if abs(r[0].center_y - fragment.center_y)
                        <= min(r[0].height, fragment.height) * .45), None)
            if row is None:
                rows.append([fragment])
            else:
                row.append(fragment)
        lines = []
        for row in rows:
            group: list[_Text] = []
            for part in sorted(row, key=lambda p: p.bbox[0]):
                if group and part.bbox[0] - group[-1].bbox[2] > max(part.height, group[-1].height) * 1.8:
                    lines.append(_merge(group))
                    group = []
                group.append(part)
            lines.append(_merge(group))
        return sorted(lines, key=lambda p: (-p.center_y, p.bbox[0]))

    def _role(self, text: str) -> str | None:
        """Require a whole heading; narrative role mentions are not labels."""
        return _ROLES.get(re.sub(r"\s+", " ", text.upper().strip().rstrip(":")))

    def _headings(self, fragments: list[_Text]) -> list[_Text]:
        """Recognize intact, split-word, and two-line role headings."""
        candidates = fragments + self._lines(fragments)
        for first in self._lines(fragments):
            if first.text.upper() not in {"GENERAL", "PROJECT", "STRUCTURAL", "CIVIL", "PROPERTY", "LANDSCAPE"}:
                continue
            for second in fragments:
                gap = first.bbox[1] - second.bbox[3]
                if -.2 * first.height <= gap <= first.height and abs(first.bbox[0] - second.bbox[0]) < first.height:
                    candidates.append(_merge([first, second]))
        headings: list[_Text] = []
        for candidate in sorted(candidates, key=lambda p: -len(p.text)):
            if self._role(candidate.text) and not any(candidate.ids & old.ids for old in headings):
                headings.append(candidate)
        return sorted(headings, key=lambda p: (-p.center_y, p.bbox[0]))

    def _panel(self, heading: _Text, headings: list[_Text], fragments: list[_Text], width: float) -> list[_Text]:
        """Bound columns by peer labels and stop at the next vertical panel."""
        peers = sorted((h for h in headings if abs(h.center_y - heading.center_y) <= max(h.height, heading.height)), key=lambda p: p.bbox[0])
        index = peers.index(heading)
        left = heading.bbox[0] - heading.height * .5
        if index + 1 < len(peers):
            right = peers[index + 1].bbox[0] - heading.height * .4
        elif index:
            right = min(width, heading.bbox[0] + heading.bbox[0] - peers[index - 1].bbox[0])
        else:
            right = min(width, heading.bbox[0] + max(heading.bbox[2] - heading.bbox[0], heading.height * 25))
        bottom = heading.bbox[1] - heading.height * 16
        for other in headings:
            if other.center_y < heading.bbox[1] and left <= other.bbox[0] < right:
                bottom = max(bottom, other.bbox[3])
        selected = [p for p in fragments if not (p.ids & heading.ids)
                    and left <= p.bbox[0] < right and p.bbox[2] <= right + heading.height * .25
                    and bottom < p.center_y < heading.bbox[1]]
        result = [heading]
        previous_bottom = heading.bbox[1]
        for line in self._lines(selected):
            # A nearby unlabeled column is not automatically this role's detail.
            if line.bbox[0] > heading.bbox[0] + heading.height * 3:
                continue
            if previous_bottom - line.bbox[3] > heading.height * 3:
                break
            result.append(line)
            previous_bottom = min(previous_bottom, line.bbox[1])
        return result

    def _page(self, page: dict) -> tuple[list[dict], list[dict]]:
        """Resolve labelled panels and preserve unassigned contact signals."""
        fragments = self._fragments(page)
        headings = self._headings(fragments)
        contacts, unresolved, assigned = [], [], set()
        for heading in headings:
            panel = self._panel(heading, headings, fragments, float(page.get("width", 10000)))
            evidence = self._evidence(panel, page)
            contact = self._contact(panel, evidence)
            if contact:
                contacts.append(contact)
                assigned.update(set().union(*(p.ids for p in panel)))
            elif self._has_details(evidence["evidence_snippet"]):
                flags = ["contact_association_unresolved"]
                if _STAFF.search(evidence["evidence_snippet"]):
                    flags.append("city_staff_not_developer")
                unresolved.append({**evidence, "review_flags": flags})
                assigned.update(set().union(*(p.ids for p in panel)))
        unresolved.extend(self._unlabelled(fragments, assigned, page))
        return contacts, unresolved

    def _evidence(self, panel: list[_Text], page: dict) -> dict:
        """Keep exact selected text, page and geometric provenance."""
        methods = {part.method for part in panel}
        scores = [part.ocr_confidence for part in panel if part.ocr_confidence is not None]
        return {"evidence_snippet": "\n".join(part.text for part in panel),
                "pdf_page": page.get("page"), "bbox": list(_union(panel)),
                "extraction_method": methods.pop() if len(methods) == 1 else "mixed",
                "ocr_confidence_min": min(scores) if scores else None}

    def _has_details(self, text: str) -> bool:
        """Identify contact-like text without assigning an organization."""
        return bool(self._phone_values(text) or _EMAIL.search(text) or self._website(text))

    def _website(self, text: str) -> str | None:
        """Join split URL punctuation while leaving the source evidence intact."""
        for line in _EMAIL.sub("", text).splitlines():
            if re.search(r"\b(?:www\.|https?://)", line, re.I):
                # PDF spans may add a space before a domain's printed hyphen.
                line = re.sub(r"(?<=\w)[ \t]*([.-])[ \t]*(?=\w)", r"\1", line)
            match = _WEB.search(line)
            if match:
                return match.group()
        return None

    def _phone_values(self, text: str) -> list[str]:
        """Preserve labeled malformed numbers as well as conventional phones."""
        result = []
        for line in text.splitlines():
            labelled = _LABELLED_PHONE.fullmatch(line.strip())
            if labelled:
                result.append(labelled.group(1).strip())
            else:
                result.extend(match.group() for match in _PHONE.finditer(line))
        return result

    def _unlabelled(self, fragments: list[_Text], assigned: set, page: dict) -> list[dict]:
        """Keep nearby unlabeled evidence separate from resolved contacts."""
        pending = []
        lines = self._lines([p for p in fragments if not p.ids & assigned])
        seen: set[int] = set()
        for part in lines:
            if not self._has_details(part.text) or part.ids & seen:
                continue
            # Nearby rows provide review context, without making an association.
            column = [p for p in lines if abs(p.bbox[0] - part.bbox[0]) <= part.height
                      and abs(p.center_y - part.center_y) <= part.height * 5]
            column.sort(key=lambda p: -p.center_y)
            index = column.index(part)
            start, end = index, index + 1
            while start > 0 and self._nearby(column[start - 1], column[start]):
                start -= 1
            while end < len(column) and self._nearby(column[end - 1], column[end]):
                end += 1
            panel = column[start:end]
            evidence = self._evidence(panel, page)
            flags = ["missing_role_or_company_association"]
            if _STAFF.search(evidence["evidence_snippet"]):
                flags.append("city_staff_not_developer")
            pending.append({**evidence, "review_flags": flags})
            seen.update(set().union(*(p.ids for p in panel)))
        return pending

    def _nearby(self, upper: _Text, lower: _Text) -> bool:
        """Stop unresolved context at a large gap or a new explicit role."""
        return (not self._role(lower.text)
                and upper.bbox[1] - lower.bbox[3] <= max(upper.height, lower.height) * 2)

    def _fields(self, panel: list[_Text]) -> dict:
        """Parse only fields inside this one panel; never repair source text."""
        text = "\n".join(p.text for p in panel[1:])
        phones, email = self._phone_values(text), _EMAIL.search(text)
        website = self._website(text)
        address = []
        for line in panel[1:]:
            if _STREET.search(line.text) or (address and (_CITY.search(line.text) or _SUITE.search(line.text))):
                address.append(line.text)
        return {"phone": phones[0] if phones else None,
                "email": email.group() if email else None,
                "website": website,
                "mailing_address": "\n".join(address) or None}

    def _identity(self, panel: list[_Text]) -> tuple[str | None, str | None]:
        """Use the first short identity line after the explicit role label."""
        company, name = None, None
        for line in panel[1:3]:
            text = line.text.strip()
            if _STREET.search(text) or _CITY.search(text) or self._has_details(text):
                break
            if len(text) > 100 or len(text.split()) > 12 or self._role(text):
                break
            if _PERSON.search(text):
                name = text
            elif company is None:
                company = text
            else:
                break
        return company, name

    def _contact(self, panel: list[_Text], evidence: dict) -> dict | None:
        """Only explicit role panels can become contacts; preserve uncertainty."""
        if len(panel) < 2 or _STAFF.search(evidence["evidence_snippet"]):
            return None
        fields = self._fields(panel)
        company, name = self._identity(panel)
        if not (company or name) or not any(fields.values()):
            return None
        flags = []
        phone = fields["phone"]
        digits = re.sub(r"\D", "", re.split(r"(?:ext\.?|x)", phone or "", flags=re.I)[0])
        valid_phone = len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))
        if phone and not valid_phone:
            flags.append("nonstandard_phone_in_source")
        if len(set(self._phone_values(evidence["evidence_snippet"]))) > 1:
            flags.append("multiple_phone_numbers_require_review")
        if evidence["extraction_method"] != "text_layer":
            flags.append("ocr_evidence_requires_review")
        if evidence["ocr_confidence_min"] is not None and evidence["ocr_confidence_min"] < .7:
            flags.append("low_ocr_confidence")
        if not fields["email"] and not valid_phone:
            flags.append("no_verified_direct_contact")
        role = self._role(panel[0].text)
        return {"role": role, "source_role": panel[0].text, "company": company,
                "name": name, **fields, **evidence, "review_flags": flags,
                "confidence": "needs_review" if flags else "source_grounded",
                "useful_for_outreach": role in {"owner", "developer", "applicant"}
                and bool(fields["email"] or valid_phone) and not flags}
