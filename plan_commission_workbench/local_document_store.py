"""Persist compact extraction evidence without retaining downloaded documents."""

from __future__ import annotations

from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import sqlite3
from typing import Any


class LocalDocumentStore:
    """Purpose: reuse evidence by source identity and extraction configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS document_evidence ("
                "source_url TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL, "
                "created_utc TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (source_url, version))"
            )

    def get(self, source_url: str, version: str) -> dict[str, Any] | None:
        """Purpose: consult previously read source identities before downloading."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM document_evidence WHERE source_url = ? AND version = ?",
                (source_url, version),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, source_url: str, version: str, payload: dict[str, Any]) -> None:
        """Purpose: store only bounded contact evidence and coverage metadata."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO document_evidence VALUES (?, ?, ?, ?, ?)",
                (source_url, version, payload["content_hash"], dt.datetime.now(dt.UTC).isoformat(), json.dumps(payload)),
            )

    def _connect(self) -> sqlite3.Connection:
        """Purpose: use short-lived connections safely across server requests."""

        return sqlite3.connect(self.path, timeout=30)
