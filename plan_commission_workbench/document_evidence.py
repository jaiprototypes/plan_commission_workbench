"""Local PDF evidence with page geometry, explicit coverage, and bounded OCR."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Callable


PDFIUM_LOCK = threading.RLock()


class DocumentEvidenceError(RuntimeError):
    """Purpose: stop the caller when reading or local OCR fails."""


class DocumentEvidenceReader:
    """Purpose: read all PDF pages without sending documents to a hosted model."""

    def __init__(self, *, ocr_backend: Callable | None = None, timeout_seconds: float = 600) -> None:
        self.ocr_backend = ocr_backend or LocalOcrWorker(timeout_seconds=timeout_seconds).read

    def read(self, path: Path, *, use_ocr: bool = True) -> dict:
        """Purpose: preserve local text and report every page needing visual reading."""
        try:
            pages = self._read_pages(Path(path))
            requested = [page["page"] for page in pages if page["coverage"] == "needs_ocr"]
            if requested and use_ocr:
                self._merge_ocr(pages, self.ocr_backend(Path(path), requested), requested)
            elif requested:
                for page in pages:
                    if page["page"] in requested:
                        page["issues"].append("ocr_disabled")
            warnings = [f"Page {p['page']}: {issue}" for p in pages for issue in p["issues"]]
            return {
                "page_count": len(pages), "pages": pages,
                "text_chars": sum(len(b["text"]) for p in pages for b in p["blocks"]),
                "coverage_complete": all(p["coverage"] in {"text_layer", "blank", "ocr_completed"} for p in pages),
                "warnings": warnings,
            }
        except DocumentEvidenceError:
            raise
        except Exception as exc:
            raise DocumentEvidenceError(f"Local document reading failed for {Path(path).name}: {exc}") from exc

    def _read_pages(self, path: Path) -> list[dict]:
        """Purpose: close native PDFium objects promptly after each page."""
        import pypdfium2 as pdfium

        pages = []
        with PDFIUM_LOCK:
            with pdfium.PdfDocument(str(path)) as document:
                for index in range(len(document)):
                    with closing(document[index]) as page:
                        pages.append(self._read_page(page, index + 1))
        if not pages:
            raise DocumentEvidenceError(f"PDF has no pages: {path.name}")
        return pages

    def _read_page(self, page, number: int) -> dict:
        """Purpose: retain fine text spans so neighboring columns remain separate."""
        width, height = page.get_size()
        with closing(page.get_textpage()) as textpage:
            blocks = self._text_blocks(textpage)
        objects = list(page.get_objects())
        # Images can contain the body even when the footer has abundant text.
        image_count = sum(obj.type == 3 for obj in objects)
        has_graphics = any(obj.type in {2, 3, 4, 5} for obj in objects)
        needs_ocr = bool(image_count or (has_graphics and not blocks))
        issues = ["raster_content_requires_visual_reading"] if image_count else []
        if has_graphics and not blocks and not image_count:
            issues.append("graphics_without_text_require_visual_reading")
        if blocks and has_graphics and not image_count:
            # Vector outlines may encode lettering. Text extraction cannot prove otherwise.
            issues.append("vector_graphics_not_interpreted")
        coverage = "needs_ocr" if needs_ocr else ("text_layer" if blocks else "blank")
        if "vector_graphics_not_interpreted" in issues:
            coverage = "text_layer_partial"
        return {"page": number, "width": width, "height": height, "blocks": blocks,
                "coverage": coverage, "issues": issues, "image_count": image_count}

    def _text_blocks(self, textpage) -> list[dict]:
        """Purpose: split character sequences at line or column boundaries."""
        blocks, chars, boxes = [], [], []
        for index in range(textpage.count_chars()):
            text = textpage.get_text_range(index, 1)
            if not text or text in {"\r", "\n"}:
                self._flush_span(blocks, chars, boxes)
                continue
            bbox = list(textpage.get_charbox(index))
            if boxes and self._separate_span(boxes[-1], bbox):
                self._flush_span(blocks, chars, boxes)
            chars.append(text)
            if text.strip():
                boxes.append(bbox)
        self._flush_span(blocks, chars, boxes)
        return blocks

    @staticmethod
    def _separate_span(previous: list[float], current: list[float]) -> bool:
        """Purpose: avoid joining different baselines or neighboring contact columns."""
        height = max(previous[3] - previous[1], current[3] - current[1], 1)
        return (abs(current[1] - previous[1]) > height * .65
                or current[0] - previous[2] > height * 1.8
                or current[2] < previous[0] - height)

    @staticmethod
    def _flush_span(blocks: list, chars: list, boxes: list) -> None:
        """Purpose: materialize a bounded span without whitespace-only objects."""
        text = "".join(chars).strip()
        if text and boxes:
            bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes)]
            blocks.append({"text": text, "bbox": bbox, "method": "text_layer"})
        chars.clear()
        boxes.clear()

    @staticmethod
    def _merge_ocr(pages: list[dict], result: dict, requested: list[int]) -> None:
        """Purpose: require explicit page results and preserve uncertain OCR coverage."""
        if not isinstance(result, dict) or any(str(number) not in result for number in requested):
            raise DocumentEvidenceError("Local OCR omitted requested page results")
        for page in pages:
            if page["page"] not in requested:
                continue
            output = result[str(page["page"])]
            blocks = output.get("blocks")
            if not isinstance(blocks, list):
                raise DocumentEvidenceError(f"Invalid OCR blocks on page {page['page']}")
            for block in blocks:
                if not isinstance(block.get("text"), str) or len(block.get("bbox", [])) != 4:
                    raise DocumentEvidenceError(f"Invalid OCR evidence on page {page['page']}")
                block["method"] = "ocr"
            for block in blocks:
                if not any(DocumentEvidenceReader._duplicate(block, old) for old in page["blocks"]):
                    page["blocks"].append(block)
            page["issues"] = output.get("issues", [])
            # No recognized text on a raster page is an unresolved search, not success.
            if not blocks:
                page["issues"].append("ocr_found_no_text_in_visual_content")
            page["coverage"] = "ocr_partial" if page["issues"] else "ocr_completed"

    @staticmethod
    def _duplicate(block: dict, old: dict) -> bool:
        """Purpose: deduplicate OCR overlap while preserving repeated text elsewhere."""
        return (" ".join(block["text"].casefold().split()) == " ".join(old["text"].casefold().split())
                and abs(block["bbox"][0] - old["bbox"][0]) < 8
                and abs(block["bbox"][1] - old["bbox"][1]) < 8)


class LocalOcrWorker:
    """Purpose: isolate CPU OCR so failed or hung recognition stops predictably."""

    def __init__(self, *, timeout_seconds: float = 600) -> None:
        self.timeout_seconds = timeout_seconds

    def read(self, path: Path, pages: list[int]) -> dict:
        """Purpose: run installed local models with a deadline and ephemeral artifacts."""
        with tempfile.TemporaryDirectory(prefix="pcw-local-ocr-") as workdir:
            output = Path(workdir) / "evidence.json"
            command = self._command(path, pages, output)
            env = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
            try:
                process = subprocess.run(command, capture_output=True, text=True, env=env,
                                         timeout=self.timeout_seconds, check=False)
            except subprocess.TimeoutExpired as exc:
                raise DocumentEvidenceError(f"Local OCR timed out after {self.timeout_seconds:g} seconds") from exc
            if process.returncode:
                raise DocumentEvidenceError(f"Local OCR failed: {process.stderr[-1500:].strip()}")
            try:
                return json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DocumentEvidenceError("Local OCR did not produce valid evidence JSON") from exc

    @staticmethod
    def _command(path: Path, pages: list[int], output: Path) -> list[str]:
        """Purpose: support both source Python and the packaged desktop dispatcher."""
        args = ["--pdf", str(path.resolve()), "--pages", ",".join(map(str, pages)), "--output-json", str(output)]
        if getattr(sys, "frozen", False):
            return [sys.executable, "--document-evidence-worker", *args]
        return [sys.executable, "-m", "plan_commission_workbench.document_evidence_worker", *args]
