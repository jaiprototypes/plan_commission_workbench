"""SQLite persistence for runs, source records, review, and export."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import traceback
from pathlib import Path
from typing import Any, Iterable

from . import statuses
from .models import AgendaClassification, AgendaSegment, ApplicationExtraction, FieldEvidence


def _now() -> str:
    """Purpose: produce compact UTC timestamps for SQLite rows."""

    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    """Purpose: parse stored UTC timestamps for watchdog age checks."""

    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC)


def _pid_alive(pid: int | None) -> bool | None:
    """Purpose: check whether a recorded worker process still exists."""

    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Purpose: convert sqlite rows without leaking sqlite-specific objects."""

    return dict(row) if row else None


class ReviewStore:
    """Purpose: own all short SQLite transactions for the workbench."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        """Purpose: create the workbench-only schema and dedupe indexes."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            self._ensure_columns(conn)

    def backup_to(self, destination: Path) -> Path:
        """Purpose: create a consistent DB copy while the server may be running."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.db_path, timeout=30)
        target = sqlite3.connect(destination, timeout=30)
        try:
            source.execute("PRAGMA busy_timeout = 30000")
            target.execute("PRAGMA busy_timeout = 30000")
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Purpose: apply small forward-compatible SQLite migrations."""

        app_columns = self._table_columns(conn, "application_extractions")
        if "target_project" not in app_columns:
            conn.execute("ALTER TABLE application_extractions ADD COLUMN target_project INTEGER")
        if "target_reason" not in app_columns:
            conn.execute("ALTER TABLE application_extractions ADD COLUMN target_reason TEXT")
        run_columns = self._table_columns(conn, "runs")
        if "worker_pid" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN worker_pid INTEGER")
        if "heartbeat_at" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
        if "heartbeat_stage" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_stage TEXT")
        if "heartbeat_source" not in run_columns:
            conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_source TEXT")
        conn.execute("DROP INDEX IF EXISTS uq_source_items_kind_hash")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_source_items_agenda_hash
            ON source_items(source_kind, content_hash)
            WHERE source_kind = 'agenda' AND content_hash IS NOT NULL AND content_hash != ''
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_source_items_application_hash
            ON source_items(source_kind, content_hash)
            WHERE source_kind = 'application' AND content_hash IS NOT NULL AND content_hash != ''
            """
        )

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        """Purpose: read SQLite table columns for tiny migrations."""

        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    @contextlib.contextmanager
    def transaction(self) -> Iterable[sqlite3.Connection]:
        """Purpose: keep write transactions short and explicit."""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_run(self, date_from: dt.date, date_to: dt.date, request_text: str | None) -> int:
        """Purpose: insert a new Madison run row."""

        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (date_from, date_to, run_request_text, status, created_at, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (date_from.isoformat(), date_to.isoformat(), request_text, statuses.RUNNING, _now(), _now()),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str = statuses.COMPLETED,
        last_error: str | None = None,
        *,
        only_if_running: bool = True,
    ) -> bool:
        """Purpose: close a run with its final status."""

        with self.transaction() as conn:
            query = "UPDATE runs SET status = ?, last_error = ?, finished_at = ? WHERE id = ?"
            params: tuple[Any, ...] = (status, last_error, _now(), run_id)
            if only_if_running:
                query += " AND status = ?"
                params = (*params, statuses.RUNNING)
            cur = conn.execute(query, params)
            return bool(cur.rowcount)

    def register_run_worker(self, run_id: int, worker_pid: int) -> None:
        """Purpose: record the process responsible for a background scrape."""

        stamp = _now()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                SET worker_pid = ?, heartbeat_at = ?, heartbeat_stage = ?, heartbeat_source = NULL
                WHERE id = ? AND status = ?
                """,
                (worker_pid, stamp, "worker_start", run_id, statuses.RUNNING),
            )
            conn.execute(
                """
                INSERT INTO run_events
                (run_id, stage, component, source_identity, message, traceback_summary, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, "worker_start", "runner", None, f"Worker PID {worker_pid} started run", None, stamp),
            )

    def heartbeat_run(
        self,
        run_id: int,
        stage: str,
        component: str,
        source_identity: str | None,
        message: str,
    ) -> bool:
        """Purpose: mark a live run's current long-running stage."""

        stamp = _now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET heartbeat_at = ?, heartbeat_stage = ?, heartbeat_source = ?
                WHERE id = ? AND status = ?
                """,
                (stamp, stage, source_identity, run_id, statuses.RUNNING),
            )
            if not cur.rowcount:
                return False
            conn.execute(
                """
                INSERT INTO run_events
                (run_id, stage, component, source_identity, message, traceback_summary, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, stage, component, source_identity, message, None, stamp),
            )
            return True

    def run_is_running(self, run_id: int) -> bool:
        """Purpose: let workers stop writing after watchdog failure."""

        with self.transaction() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            return bool(row and row["status"] == statuses.RUNNING)

    def mark_stale_running_runs(self, stale_after_seconds: int) -> list[dict[str, Any]]:
        """Purpose: fail running rows whose worker died or stopped heartbeating."""

        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        marked: list[dict[str, Any]] = []
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE status = ?", (statuses.RUNNING,)).fetchall()
            for row in rows:
                result = self._stale_result(dict(row), now, stale_after_seconds)
                if not result:
                    continue
                status = self._failure_status_for_stage(row["heartbeat_stage"])
                message = self._stale_message(row, result, stale_after_seconds)
                stamp = now.isoformat().replace("+00:00", "Z")
                cur = conn.execute(
                    """
                    UPDATE runs
                    SET status = ?, last_error = ?, finished_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (status, message, stamp, row["id"], statuses.RUNNING),
                )
                if not cur.rowcount:
                    continue
                conn.execute(
                    """
                    INSERT INTO run_events
                    (run_id, stage, component, source_identity, message, traceback_summary, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (row["id"], status, "watchdog", row["heartbeat_source"], message, None, stamp),
                )
                worker_spawned = bool(
                    conn.execute(
                        "SELECT 1 FROM run_events WHERE run_id = ? AND stage = ? LIMIT 1",
                        (row["id"], "worker_spawn"),
                    ).fetchone()
                )
                marked.append(
                    {
                        "run_id": row["id"],
                        "status": status,
                        "message": message,
                        "worker_pid": row["worker_pid"],
                        "pid_alive": result["pid_alive"],
                        "reason": result["reason"],
                        "worker_spawned": worker_spawned,
                    }
                )
        return marked

    def _stale_result(self, row: dict[str, Any], now: dt.datetime, stale_after_seconds: int) -> dict[str, Any] | None:
        """Purpose: decide whether a running row is stale or dead."""

        pid_alive = _pid_alive(row.get("worker_pid"))
        last_seen = _parse_timestamp(row.get("heartbeat_at") or row.get("started_at") or row.get("created_at"))
        age_seconds = int((now - last_seen).total_seconds()) if last_seen else stale_after_seconds + 1
        if pid_alive is False:
            return {"reason": "worker process is no longer alive", "age_seconds": age_seconds, "pid_alive": pid_alive}
        if age_seconds >= stale_after_seconds:
            return {"reason": "heartbeat timed out", "age_seconds": age_seconds, "pid_alive": pid_alive}
        return None

    def _failure_status_for_stage(self, stage: str | None) -> str:
        """Purpose: map a stale heartbeat to the most useful run failure."""

        value = stage or ""
        if value == statuses.APPLICATION_DOWNLOADING:
            return statuses.FAILED_APPLICATION_DOWNLOAD
        if value == statuses.APPLICATION_DOCLING:
            return statuses.FAILED_APPLICATION_DOCLING
        if value == statuses.APPLICATION_LLM_EXTRACTING:
            return statuses.FAILED_APPLICATION_LLM
        if value == statuses.AGENDA_CLASSIFYING:
            return statuses.FAILED_AGENDA_LLM
        if value == "agenda_downloading":
            return statuses.FAILED_AGENDA_DOCLING
        if value == "agenda_docling":
            return statuses.FAILED_AGENDA_DOCLING
        if value.startswith("application"):
            return statuses.FAILED_APPLICATION_LLM
        return statuses.FAILED_AGENDA_LLM

    def _stale_message(self, row: sqlite3.Row, result: dict[str, Any], stale_after_seconds: int) -> str:
        """Purpose: create an operator-readable watchdog failure."""

        return (
            "Run watchdog stopped stale processing: "
            f"{result['reason']}; stage={row['heartbeat_stage'] or 'unknown'}; "
            f"source={row['heartbeat_source'] or 'unknown'}; worker_pid={row['worker_pid'] or 'unknown'}; "
            f"pid_alive={result['pid_alive']}; heartbeat_age_seconds={result['age_seconds']}; "
            f"limit_seconds={stale_after_seconds}"
        )

    def update_counters(self, run_id: int) -> None:
        """Purpose: refresh counters for the run's requested date range."""

        with self.transaction() as conn:
            run = conn.execute("SELECT date_from, date_to FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                return
            date_from = run["date_from"]
            date_to = run["date_to"]
            agenda_total = conn.execute(
                "SELECT COUNT(*) FROM agenda_items WHERE meeting_date BETWEEN ? AND ?",
                (date_from, date_to),
            ).fetchone()[0]
            agenda_hits = conn.execute(
                """
                SELECT COUNT(*) FROM agenda_items
                WHERE meeting_date BETWEEN ? AND ? AND classification = ?
                """,
                (date_from, date_to, statuses.AGENDA_HIT),
            ).fetchone()[0]
            app_total = conn.execute(
                """
                SELECT COUNT(*) FROM application_extractions app
                JOIN agenda_items agenda ON agenda.id = app.agenda_item_id
                WHERE agenda.meeting_date BETWEEN ? AND ?
                """,
                (date_from, date_to),
            ).fetchone()[0]
            app_done = conn.execute(
                """
                SELECT COUNT(*) FROM application_extractions app
                JOIN agenda_items agenda ON agenda.id = app.agenda_item_id
                WHERE agenda.meeting_date BETWEEN ? AND ? AND app.status IN (?, ?, ?, ?)
                """,
                (
                    date_from,
                    date_to,
                    statuses.APPLICATION_EXTRACTED,
                    statuses.NEEDS_OPERATOR_REVIEW,
                    statuses.ACCEPTED,
                    statuses.REJECTED,
                ),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE runs
                SET agenda_total = ?, agenda_hits = ?, applications_total = ?, applications_extracted = ?
                WHERE id = ?
                """,
                (agenda_total, agenda_hits, app_total, app_done, run_id),
            )

    def fail_run_from_exception(self, run_id: int, status: str, exc: BaseException) -> None:
        """Purpose: mark failures and keep a traceback summary for operators."""

        self.log_event(
            run_id,
            stage=status,
            component="runner",
            source_identity=None,
            message=str(exc),
            traceback_summary="".join(traceback.format_exception_only(type(exc), exc)).strip(),
        )
        self.finish_run(run_id, status=status, last_error=str(exc))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        """Purpose: fetch one run for API, CLI, and retry."""

        with self.transaction() as conn:
            return _dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Purpose: show recent run history."""

        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def log_event(
        self,
        run_id: int,
        stage: str,
        component: str,
        source_identity: str | None,
        message: str,
        traceback_summary: str | None = None,
    ) -> None:
        """Purpose: append operator-readable run diagnostics."""

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO run_events
                (run_id, stage, component, source_identity, message, traceback_summary, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, stage, component, source_identity, message, traceback_summary, _now()),
            )

    def list_run_events(self, run_id: int) -> list[dict[str, Any]]:
        """Purpose: return one run's log stream."""

        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
            return [dict(row) for row in rows]

    def find_source(self, source_kind: str, source_url: str | None, content_hash: str | None) -> dict[str, Any] | None:
        """Purpose: locate prior source records by URL first, then hash."""

        with self.transaction() as conn:
            if source_url:
                row = conn.execute(
                    "SELECT * FROM source_items WHERE source_kind = ? AND source_url = ?",
                    (source_kind, source_url),
                ).fetchone()
                if row:
                    return dict(row)
            if content_hash and source_kind != "application":
                row = conn.execute(
                    "SELECT * FROM source_items WHERE source_kind = ? AND content_hash = ?",
                    (source_kind, content_hash),
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def upsert_source_item(
        self,
        *,
        run_id: int,
        source_kind: str,
        event_id: str | None,
        file_id: str | None,
        attachment_id: str | None,
        source_url: str | None,
        content_hash: str | None,
        processing_status: str,
    ) -> int:
        """Purpose: persist source identity without duplicating work."""

        existing = self.find_source(source_kind, source_url, content_hash)
        with self.transaction() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE source_items
                    SET run_id = ?, event_id = COALESCE(?, event_id), file_id = COALESCE(?, file_id),
                        attachment_id = COALESCE(?, attachment_id), content_hash = COALESCE(?, content_hash),
                        processing_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        run_id,
                        event_id,
                        file_id,
                        attachment_id,
                        content_hash,
                        processing_status,
                        _now(),
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO source_items
                (run_id, source_kind, event_id, file_id, attachment_id, source_url, content_hash,
                 processing_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_kind,
                    event_id,
                    file_id,
                    attachment_id,
                    source_url,
                    content_hash,
                    processing_status,
                    _now(),
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def set_source_status(self, source_id: int, status: str) -> None:
        """Purpose: update source processing progress."""

        with self.transaction() as conn:
            conn.execute(
                "UPDATE source_items SET processing_status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), source_id),
            )

    def agenda_complete(self, event_id: str, source_url: str | None = None, content_hash: str | None = None) -> bool:
        """Purpose: decide whether agenda Docling and LLM work can be skipped."""

        source = self.find_source("agenda", source_url, content_hash)
        if not source or source["processing_status"] not in statuses.AGENDA_FINAL_STATUSES:
            return False
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM agenda_items
                WHERE event_id = ? AND classification IN (?, ?, ?)
                """,
                (event_id, statuses.AGENDA_HIT, statuses.NOT_TARGET_PROJECT, statuses.NEEDS_AGENDA_REVIEW),
            ).fetchone()
            return bool(row and row["total"])

    def upsert_agenda_item(
        self,
        run_id: int,
        source_item_id: int,
        segment: AgendaSegment,
        classification: AgendaClassification,
    ) -> int:
        """Purpose: upsert classified agenda item rows by event and city item."""

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM agenda_items WHERE event_id = ? AND city_item_id = ?",
                (segment.event_id, segment.city_item_id),
            ).fetchone()
            values = (
                run_id,
                source_item_id,
                segment.file_id,
                segment.meeting_date.isoformat(),
                segment.description,
                classification.classification,
                classification.confidence,
                classification.reason,
                classification.evidence_snippet,
                _now(),
            )
            if row:
                conn.execute(
                    """
                    UPDATE agenda_items
                    SET run_id = ?, source_item_id = ?, file_id = ?, meeting_date = ?, description = ?,
                        classification = ?, confidence = ?, reason = ?, evidence_snippet = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO agenda_items
                (run_id, source_item_id, event_id, file_id, city_item_id, meeting_date, description,
                 classification, confidence, reason, evidence_snippet, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_item_id,
                    segment.event_id,
                    segment.file_id,
                    segment.city_item_id,
                    segment.meeting_date.isoformat(),
                    segment.description,
                    classification.classification,
                    classification.confidence,
                    classification.reason,
                    classification.evidence_snippet,
                    _now(),
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def list_agenda_items(self, status: str | None = None) -> list[dict[str, Any]]:
        """Purpose: fetch classified agenda items for review screens."""

        query = "SELECT * FROM agenda_items"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE classification = ?"
            params = (status,)
        query += " ORDER BY meeting_date DESC, event_id, city_item_id"
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_agenda_hits_for_dates(self, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
        """Purpose: find hit rows that may need application extraction."""

        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agenda_items
                WHERE classification = ? AND meeting_date BETWEEN ? AND ?
                ORDER BY meeting_date, event_id, city_item_id
                """,
                (statuses.AGENDA_HIT, date_from.isoformat(), date_to.isoformat()),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_agenda_needs_review(self, agenda_item_id: int, reason: str) -> bool:
        """Purpose: keep invalid historical hit rows from enqueueing applications."""

        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE agenda_items
                SET classification = ?, confidence = 0, reason = ?, evidence_snippet = ?, updated_at = ?
                WHERE id = ? AND classification = ?
                """,
                (statuses.NEEDS_AGENDA_REVIEW, reason, reason[:240], _now(), agenda_item_id, statuses.AGENDA_HIT),
            )
            return bool(cur.rowcount)

    def application_complete(self, agenda_item_id: int, source_url: str | None = None, attachment_id: str | None = None) -> bool:
        """Purpose: decide whether application Docling and LLM work can be skipped."""

        clauses = ["agenda_item_id = ?", "status IN (?, ?, ?)"]
        params: list[Any] = [agenda_item_id, statuses.APPLICATION_EXTRACTED, statuses.ACCEPTED, statuses.REJECTED]
        if source_url:
            clauses.append("source_url = ?")
            params.append(source_url)
        if attachment_id:
            clauses.append("attachment_id = ?")
            params.append(attachment_id)
        with self.transaction() as conn:
            row = conn.execute(
                f"SELECT id FROM application_extractions WHERE {' AND '.join(clauses)} LIMIT 1",
                tuple(params),
            ).fetchone()
            return bool(row)

    def upsert_application_extraction(
        self,
        run_id: int,
        source_item_id: int,
        extraction: ApplicationExtraction,
    ) -> int:
        """Purpose: persist successful normalized application extraction rows."""

        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT id FROM application_extractions
                WHERE agenda_item_id = ? AND (source_url = ? OR attachment_id = ?)
                """,
                (extraction.agenda_item_id, extraction.source_url, extraction.attachment_id),
            ).fetchone()
            values = self._application_values(run_id, source_item_id, extraction)
            if row:
                conn.execute(APPLICATION_UPDATE_SQL, (*values, _now(), row["id"]))
                extraction_id = int(row["id"])
            else:
                stamp = _now()
                cur = conn.execute(APPLICATION_INSERT_SQL, (*values, stamp, stamp))
                extraction_id = int(cur.lastrowid)
            self._replace_field_evidence(conn, extraction_id, extraction.evidence)
            return extraction_id

    def _application_values(
        self,
        run_id: int,
        source_item_id: int,
        extraction: ApplicationExtraction,
    ) -> tuple[Any, ...]:
        """Purpose: flatten dataclass fields for SQLite writes."""

        return (
            run_id,
            source_item_id,
            extraction.agenda_item_id,
            extraction.source_url,
            extraction.attachment_id,
            extraction.applicant.name,
            extraction.applicant.company,
            extraction.applicant.mailing_address,
            extraction.applicant.phone,
            extraction.applicant.email,
            extraction.project_contact.name,
            extraction.project_contact.company,
            extraction.project_contact.mailing_address,
            extraction.project_contact.phone,
            extraction.project_contact.email,
            extraction.owner.name,
            extraction.owner.company,
            extraction.owner.mailing_address,
            extraction.owner.phone,
            extraction.owner.email,
            extraction.section5_description,
            extraction.unit_count,
            extraction.status,
            None if extraction.target_project is None else int(extraction.target_project),
            extraction.target_reason,
        )

    def _replace_field_evidence(
        self,
        conn: sqlite3.Connection,
        extraction_id: int,
        evidence: tuple[FieldEvidence, ...],
    ) -> None:
        """Purpose: keep evidence current with the latest extraction."""

        conn.execute("DELETE FROM field_evidence WHERE extraction_id = ?", (extraction_id,))
        for item in evidence:
            conn.execute(
                """
                INSERT INTO field_evidence
                (extraction_id, field_name, value, evidence_snippet, confidence)
                VALUES (?, ?, ?, ?, ?)
                """,
                (extraction_id, item.field_name, None if item.value is None else str(item.value), item.evidence_snippet, item.confidence),
            )

    def list_application_extractions(self, status: str | None = None) -> list[dict[str, Any]]:
        """Purpose: fetch application rows with agenda context."""

        params: tuple[Any, ...] = ()
        where = ""
        if status:
            where = "WHERE app.status = ?"
            params = (status,)
        with self.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT app.*, agenda.event_id, agenda.city_item_id, agenda.file_id, agenda.meeting_date,
                       agenda.description AS agenda_description
                FROM application_extractions app
                JOIN agenda_items agenda ON agenda.id = app.agenda_item_id
                {where}
                ORDER BY agenda.meeting_date DESC, app.id DESC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_field_evidence(self, extraction_id: int) -> list[dict[str, Any]]:
        """Purpose: return evidence snippets for one extraction."""

        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM field_evidence WHERE extraction_id = ? ORDER BY field_name",
                (extraction_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def review_application(
        self,
        extraction_id: int,
        status: str,
        corrected_fields: dict[str, Any] | None,
        notes: str | None,
    ) -> dict[str, Any]:
        """Purpose: accept, reject, or correct one extraction."""

        if status not in {statuses.ACCEPTED, statuses.REJECTED, statuses.NEEDS_OPERATOR_REVIEW}:
            raise ValueError(f"Unsupported review status: {status}")
        corrected_json = json.dumps(corrected_fields or {}, sort_keys=True)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM application_extractions WHERE id = ?", (extraction_id,)).fetchone()
            if not row:
                raise KeyError(f"Application extraction {extraction_id} not found")
            conn.execute("UPDATE application_extractions SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), extraction_id))
            conn.execute(
                """
                INSERT INTO operator_reviews
                (extraction_id, status, corrected_fields_json, notes, reviewed_timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extraction_id) DO UPDATE SET
                    status = excluded.status,
                    corrected_fields_json = excluded.corrected_fields_json,
                    notes = excluded.notes,
                    reviewed_timestamp = excluded.reviewed_timestamp
                """,
                (extraction_id, status, corrected_json, notes, _now()),
            )
        return self.list_application_extractions()[0] if False else {"id": extraction_id, "status": status}

    def accepted_export_rows(self, status: str) -> list[dict[str, Any]]:
        """Purpose: read exportable rows only from accepted database data."""

        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT app.*, agenda.event_id, agenda.city_item_id, agenda.file_id, agenda.meeting_date,
                       agenda.description AS agenda_description, review.corrected_fields_json, review.notes
                FROM application_extractions app
                JOIN agenda_items agenda ON agenda.id = app.agenda_item_id
                LEFT JOIN operator_reviews review ON review.extraction_id = app.id
                WHERE app.status = ?
                ORDER BY agenda.meeting_date, agenda.event_id, agenda.city_item_id
                """,
                (status,),
            ).fetchall()
            return [self._apply_corrections(dict(row)) for row in rows]

    def _apply_corrections(self, row: dict[str, Any]) -> dict[str, Any]:
        """Purpose: overlay operator corrections onto export rows."""

        raw = row.pop("corrected_fields_json", None)
        if not raw:
            return row
        try:
            corrections = json.loads(raw)
        except json.JSONDecodeError:
            return row
        for key, value in corrections.items():
            if key in row and value not in (None, ""):
                row[key] = value
        return row

    def record_export(self, path: Path, fmt: str, row_count: int) -> int:
        """Purpose: persist export audit metadata."""

        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO exports (path, format, row_count, created_timestamp) VALUES (?, ?, ?, ?)",
                (str(path), fmt, row_count, _now()),
            )
            return int(cur.lastrowid)

    def get_export(self, export_id: int) -> dict[str, Any] | None:
        """Purpose: fetch one export file record for browser downloads."""

        with self.transaction() as conn:
            return _dict(conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone())


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'madison',
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    run_request_text TEXT,
    status TEXT NOT NULL,
    agenda_total INTEGER NOT NULL DEFAULT 0,
    agenda_hits INTEGER NOT NULL DEFAULT 0,
    applications_total INTEGER NOT NULL DEFAULT 0,
    applications_extracted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT,
    worker_pid INTEGER,
    heartbeat_at TEXT,
    heartbeat_stage TEXT,
    heartbeat_source TEXT
);

CREATE TABLE IF NOT EXISTS source_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    source_kind TEXT NOT NULL,
    event_id TEXT,
    file_id TEXT,
    attachment_id TEXT,
    source_url TEXT,
    content_hash TEXT,
    processing_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_items_kind_url
ON source_items(source_kind, source_url)
WHERE source_url IS NOT NULL AND source_url != '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_items_agenda_hash
ON source_items(source_kind, content_hash)
WHERE source_kind = 'agenda' AND content_hash IS NOT NULL AND content_hash != '';

CREATE INDEX IF NOT EXISTS idx_source_items_application_hash
ON source_items(source_kind, content_hash)
WHERE source_kind = 'application' AND content_hash IS NOT NULL AND content_hash != '';

CREATE TABLE IF NOT EXISTS agenda_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    source_item_id INTEGER,
    event_id TEXT NOT NULL,
    file_id TEXT,
    city_item_id TEXT NOT NULL,
    meeting_date TEXT NOT NULL,
    description TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    reason TEXT,
    evidence_snippet TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, city_item_id),
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL,
    FOREIGN KEY(source_item_id) REFERENCES source_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS application_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    source_item_id INTEGER,
    agenda_item_id INTEGER NOT NULL,
    source_url TEXT,
    attachment_id TEXT,
    applicant_name TEXT,
    applicant_company TEXT,
    applicant_mailing_address TEXT,
    applicant_phone TEXT,
    applicant_email TEXT,
    project_contact_name TEXT,
    project_contact_company TEXT,
    project_contact_mailing_address TEXT,
    project_contact_phone TEXT,
    project_contact_email TEXT,
    owner_name TEXT,
    owner_company TEXT,
    owner_mailing_address TEXT,
    owner_phone TEXT,
    owner_email TEXT,
    section5_description TEXT,
    unit_count INTEGER,
    target_project INTEGER,
    target_reason TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL,
    FOREIGN KEY(source_item_id) REFERENCES source_items(id) ON DELETE SET NULL,
    FOREIGN KEY(agenda_item_id) REFERENCES agenda_items(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_application_extractions_agenda_url
ON application_extractions(agenda_item_id, source_url)
WHERE source_url IS NOT NULL AND source_url != '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_application_extractions_agenda_attachment
ON application_extractions(agenda_item_id, attachment_id)
WHERE attachment_id IS NOT NULL AND attachment_id != '';

CREATE TABLE IF NOT EXISTS field_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    value TEXT,
    evidence_snippet TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    UNIQUE(extraction_id, field_name),
    FOREIGN KEY(extraction_id) REFERENCES application_extractions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS operator_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL,
    corrected_fields_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    reviewed_timestamp TEXT NOT NULL,
    FOREIGN KEY(extraction_id) REFERENCES application_extractions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    component TEXT NOT NULL,
    source_identity TEXT,
    message TEXT NOT NULL,
    traceback_summary TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    format TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_timestamp TEXT NOT NULL
);
"""

APPLICATION_INSERT_SQL = """
INSERT INTO application_extractions
(run_id, source_item_id, agenda_item_id, source_url, attachment_id,
 applicant_name, applicant_company, applicant_mailing_address, applicant_phone, applicant_email,
 project_contact_name, project_contact_company, project_contact_mailing_address, project_contact_phone, project_contact_email,
 owner_name, owner_company, owner_mailing_address, owner_phone, owner_email,
 section5_description, unit_count, status, target_project, target_reason, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

APPLICATION_UPDATE_SQL = """
UPDATE application_extractions
SET run_id = ?, source_item_id = ?, agenda_item_id = ?, source_url = ?, attachment_id = ?,
    applicant_name = ?, applicant_company = ?, applicant_mailing_address = ?, applicant_phone = ?, applicant_email = ?,
    project_contact_name = ?, project_contact_company = ?, project_contact_mailing_address = ?, project_contact_phone = ?, project_contact_email = ?,
    owner_name = ?, owner_company = ?, owner_mailing_address = ?, owner_phone = ?, owner_email = ?,
    section5_description = ?, unit_count = ?, status = ?, target_project = ?, target_reason = ?, updated_at = ?
WHERE id = ?
"""
