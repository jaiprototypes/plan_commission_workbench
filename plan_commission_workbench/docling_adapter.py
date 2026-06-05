"""Docling PDF text extraction adapter."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .exceptions import DoclingExtractionError


class DoclingTextExtractor:
    """Purpose: run Docling and fail hard when extraction is unavailable."""

    def __init__(self, converter_factory: Callable[[], Any] | None = None) -> None:
        self.converter_factory = converter_factory

    def extract_pdf_text(self, pdf_path: Path, output_dir: Path) -> str:
        """Purpose: extract text from a PDF into a temporary Docling folder."""

        output_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("DOCLING_CACHE_DIR", str(output_dir / "cache"))
        try:
            converter = self._converter()
            result = converter.convert(str(pdf_path))
            text = self._result_text(result)
        except Exception as exc:
            raise DoclingExtractionError(f"Docling failed for {pdf_path.name}: {exc}; {self._file_context(pdf_path)}") from exc
        if not text.strip():
            raise DoclingExtractionError(f"Docling returned empty text for {pdf_path.name}")
        sidecar = output_dir / f"{pdf_path.name}.docling.txt"
        sidecar.write_text(text, encoding="utf-8")
        return text

    def _converter(self) -> Any:
        """Purpose: instantiate Docling lazily so tests can mock it."""

        if self.converter_factory:
            return self.converter_factory()
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
        except Exception as exc:
            raise DoclingExtractionError("Docling is not installed in this environment") from exc
        return DocumentConverter()

    def _result_text(self, result: Any) -> str:
        """Purpose: support common Docling document export shapes."""

        document = getattr(result, "document", result)
        for method_name in ("export_to_markdown", "export_to_text"):
            method = getattr(document, method_name, None)
            if callable(method):
                value = method()
                if value:
                    return str(value)
        return str(document or "")

    def _file_context(self, pdf_path: Path) -> str:
        """Purpose: include disk evidence when Docling rejects an input file."""

        try:
            stat = pdf_path.stat()
            with pdf_path.open("rb") as fh:
                first_bytes = fh.read(32)
            return f"file_bytes={stat.st_size}, first_bytes={first_bytes.hex()}"
        except Exception as exc:
            return f"file_context_unavailable={exc}"
