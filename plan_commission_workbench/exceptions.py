"""Typed failures that stop the current run."""

from __future__ import annotations


class WorkbenchStop(RuntimeError):
    """Purpose: stop a run with a durable failure status."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class DoclingExtractionError(RuntimeError):
    """Purpose: identify PDF-to-text failures."""


class LLMResponseError(RuntimeError):
    """Purpose: identify missing, invalid, or non-JSON model output."""


class DownloadError(RuntimeError):
    """Purpose: identify source PDF download failures."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url

    def is_durable_missing(self) -> bool:
        """Purpose: classify broken external file links that can be skipped."""

        return self.status_code in {404, 410}
