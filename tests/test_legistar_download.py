from __future__ import annotations

from pathlib import Path

import pytest

from plan_commission_workbench.docling_adapter import DoclingTextExtractor
from plan_commission_workbench.exceptions import DoclingExtractionError, DownloadError
from plan_commission_workbench.legistar import LegistarClient


class FakeResponse:
    """Purpose: mimic the subset of requests.Response used by the downloader."""

    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.payload), chunk_size):
            yield self.payload[index : index + chunk_size]


class FakeSession:
    """Purpose: provide deterministic HTTP bytes without network access."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def mount(self, *_args) -> None:
        return None

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return self.response


def _client_for(payload: bytes, headers: dict[str, str] | None = None) -> LegistarClient:
    """Purpose: construct a Legistar client backed by fake response bytes."""

    return LegistarClient("madison", session=FakeSession(FakeResponse(payload, headers)))


def test_download_file_accepts_valid_pdf(tmp_path: Path) -> None:
    payload = b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n"
    client = _client_for(payload, {"Content-Type": "application/pdf", "Content-Length": str(len(payload))})

    downloaded = client.download_file("https://example.test/agenda.pdf", tmp_path / "agenda.pdf")

    assert downloaded.byte_count == len(payload)
    assert downloaded.content_type == "application/pdf"
    assert downloaded.first_bytes.startswith(b"%PDF-")
    assert "sha256=" in downloaded.summary()


def test_download_file_rejects_html_saved_as_pdf(tmp_path: Path) -> None:
    client = _client_for(b"<html>not a pdf</html>", {"Content-Type": "text/html"})

    with pytest.raises(DownloadError, match="not a valid PDF") as excinfo:
        client.download_file("https://example.test/agenda.pdf", tmp_path / "agenda.pdf")

    message = str(excinfo.value)
    assert "content_type=text/html" in message
    assert "first_bytes=3c68746d6c" in message


def test_download_file_rejects_truncated_response(tmp_path: Path) -> None:
    client = _client_for(b"%PDF-1.7\n", {"Content-Type": "application/pdf", "Content-Length": "999"})

    with pytest.raises(DownloadError, match="truncated"):
        client.download_file("https://example.test/agenda.pdf", tmp_path / "agenda.pdf")


def test_docling_failure_includes_file_context(tmp_path: Path) -> None:
    pdf_path = tmp_path / "agenda.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n")
    extractor = DoclingTextExtractor(converter_factory=lambda: object())

    with pytest.raises(DoclingExtractionError) as excinfo:
        extractor.extract_pdf_text(pdf_path, tmp_path / "docling")

    message = str(excinfo.value)
    assert "file_bytes=" in message
    assert "first_bytes=255044462d312e37" in message
