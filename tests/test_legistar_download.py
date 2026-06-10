from __future__ import annotations

import json
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


class FakeJsonResponse:
    """Purpose: mimic a JSON Legistar API response."""

    def __init__(self, payload) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class SequenceSession:
    """Purpose: return or raise queued values for retry tests."""

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def mount(self, *_args) -> None:
        return None

    def get(self, *_args, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


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


def test_fetch_event_items_reports_visible_retry_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SequenceSession(
        [
            TimeoutError("connection timed out"),
            FakeJsonResponse({"EventItems": [{"EventItemMatterId": "100"}]}),
        ]
    )
    messages: list[str] = []
    client = LegistarClient("madison", session=session)

    monkeypatch.setenv("PCW_LEGISTAR_JSON_ATTEMPTS", "2")
    monkeypatch.setattr("plan_commission_workbench.legistar.time.sleep", lambda _seconds: None)

    rows = client.fetch_event_items("27908", progress_callback=lambda message: messages.append(message))

    assert rows == [{"EventItemMatterId": "100"}]
    assert session.calls == 2
    assert any("request attempt 1/2" in message for message in messages)
    assert any("failed: connection timed out; retrying" in message for message in messages)
    assert any("request attempt 2/2 succeeded" in message for message in messages)


def test_fetch_event_items_fails_after_configured_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SequenceSession([TimeoutError("first"), TimeoutError("second")])
    messages: list[str] = []
    client = LegistarClient("madison", session=session)

    monkeypatch.setenv("PCW_LEGISTAR_JSON_ATTEMPTS", "2")
    monkeypatch.setattr("plan_commission_workbench.legistar.time.sleep", lambda _seconds: None)

    with pytest.raises(DownloadError, match="failed after 2 attempt"):
        client.fetch_event_items("27908", progress_callback=lambda message: messages.append(message))

    assert session.calls == 2
    assert any("failed after 2 attempt" in message for message in messages)


def test_docling_failure_includes_file_context(tmp_path: Path) -> None:
    pdf_path = tmp_path / "agenda.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n")
    extractor = DoclingTextExtractor(converter_factory=lambda: object())

    with pytest.raises(DoclingExtractionError) as excinfo:
        extractor.extract_pdf_text(pdf_path, tmp_path / "docling")

    message = str(excinfo.value)
    assert "file_bytes=" in message
    assert "first_bytes=255044462d312e37" in message


def test_docling_full_page_result_uses_mode_aware_factory(tmp_path: Path) -> None:
    pdf_path = tmp_path / "application.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n")
    modes: list[str] = []

    class FakeDocument:
        def export_to_markdown(self) -> str:
            return "Section 3 Applicant\nSection 5 Project"

    class FakeConverter:
        def convert(self, _source: str) -> FakeDocument:
            return FakeDocument()

    def factory(mode: str) -> FakeConverter:
        modes.append(mode)
        return FakeConverter()

    result = DoclingTextExtractor(converter_factory=factory).extract_pdf_text_result(
        pdf_path,
        tmp_path / "docling",
        force_full_page_ocr=True,
    )

    assert modes == ["full_page_ocr"]
    assert result.mode == "full_page_ocr"
    assert result.output_path.name == "application.pdf.full_page_ocr.docling.txt"


def test_docling_subprocess_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "application.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n")

    class HangingProcess:
        """Purpose: mimic a Docling worker that never exits."""

        pid = 12345

        def poll(self):
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout=None) -> None:
            return None

    ticks = iter([0.0, 11.0])

    monkeypatch.setenv("PCW_DOCLING_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.subprocess.Popen", lambda *_args, **_kwargs: HangingProcess())
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.os.killpg", lambda *_args: (_ for _ in ()).throw(OSError("missing group")))
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.time.sleep", lambda _seconds: None)

    with pytest.raises(DoclingExtractionError, match="timed out after 10 seconds") as excinfo:
        DoclingTextExtractor().extract_pdf_text_result(pdf_path, tmp_path / "docling")

    assert "direct worker kill requested" in str(excinfo.value)


def test_docling_subprocess_reports_progress_while_worker_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "application.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n" + (b"body\n" * 12) + b"%%EOF\n")

    class SlowSuccessProcess:
        """Purpose: mimic a slow worker that eventually writes valid JSON."""

        pid = 67890

        def __init__(self, command, **_kwargs) -> None:
            self.output_json = Path(command[command.index("--output-json") + 1])
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.poll_count >= 3:
                self.output_json.parent.mkdir(parents=True, exist_ok=True)
                self.output_json.write_text(json.dumps({"text": "Section 3\nSection 5"}), encoding="utf-8")
                return 0
            return None

    ticks = iter([0.0, 6.0, 7.0])
    messages: list[str] = []

    monkeypatch.setenv("PCW_DOCLING_WORKER_PROGRESS_SECONDS", "5")
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.subprocess.Popen", SlowSuccessProcess)
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("plan_commission_workbench.docling_adapter.time.sleep", lambda _seconds: None)

    result = DoclingTextExtractor().extract_pdf_text_result(
        pdf_path,
        tmp_path / "docling",
        progress_callback=lambda message: messages.append(message),
    )

    assert result.text == "Section 3\nSection 5"
    assert any("worker PID 67890 started" in message for message in messages)
    assert any("still running after 6s" in message for message in messages)
