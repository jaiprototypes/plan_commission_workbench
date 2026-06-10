# Plan Commission Workbench

Standalone Madison Plan Commission scrape, review, and export workbench.

This project owns Madison Legistar event access, agenda PDF processing,
agenda-hit detection, standardized Land Use Application extraction, operator
review, and accepted-row export. It is intentionally separate from the customer
DBMS. The customer DBMS should only import reviewed `.csv` or `.xlsx` output.
Reviewed accepted contacts can also be exported to `.docx` Avery 5160/8160
mailing labels for outreach.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

Set `OPENAI_API_KEY` to a credited OpenAI API key before live runs. LLM calls
are required for agenda classification and application extraction. If the key is
missing, terminal run/serve startup prompts for it when possible, and the local
Run screen prompts for a session-only key in the browser. Docling is required for
live PDF text extraction.

## CLI

```bash
pcw run madison --from 2026-01-01 --to 2026-01-31
pcw serve --host 127.0.0.1 --port 8010
pcw export --status accepted --output data/exports/madison_review.xlsx
pcw export --status accepted --output data/exports/madison_labels.docx
pcw retry --run-id 1
```

Equivalent module form:

```bash
python -m plan_commission_workbench.cli run madison --from 2026-01-01 --to 2026-01-31
```

## Server

```bash
pcw serve --host 127.0.0.1 --port 8010
```

Open `http://127.0.0.1:8010/`.

API endpoints:

- `GET /health`
- `POST /runs/madison`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /agenda-items`
- `GET /application-extractions`
- `PATCH /application-extractions/{id}/review`
- `POST /exports`
- `GET /exports/{id}/download`
- `POST /runs/{run_id}/retry`

DOCX label exports use accepted rows only. Before a contact is printed, the
workbench checks for a populated name, company, and mailing address, rejects raw
form-label text, deduplicates repeated contacts, and reports skipped contacts in
the export response. Address interpretation is handled by the LLM extraction and
operator acceptance steps, not by deterministic export parsing.

## Data

SQLite lives at `data/workbench.db`. Downloaded PDFs and Docling sidecars are
kept only in per-run temp folders under `data/tmp/` and are removed when the run
ends. Durable state is the SQLite data plus reviewed exports.

## Runtime Safeguards

Long Docling conversions run in child worker processes with hard timeouts and
visible run-log heartbeats. Useful controls:

- `PCW_DOCLING_WORKER_PROGRESS_SECONDS`: worker progress ping interval, default
  `30`.
- `PCW_DOCLING_TIMEOUT_SECONDS`: default Docling timeout, default `120`.
- `PCW_DOCLING_FULL_PAGE_TIMEOUT_SECONDS`: full-page OCR retry timeout, default
  `600`.
- `PCW_DOCLING_VLM_TIMEOUT_SECONDS`: VLM fallback timeout, default `900`.
- `PCW_LEGISTAR_TIMEOUT_SECONDS`: per-attempt Legistar HTTP timeout, default
  `30`.
- `PCW_LEGISTAR_JSON_ATTEMPTS`: visible JSON metadata attempts, default `4`.
- `PCW_RUN_STALE_SECONDS`: watchdog stale-run threshold, default `900`.

## Windows Desktop Build

The Windows launcher starts the local FastAPI server, opens the browser only
after `/health` responds at `http://127.0.0.1:8010/`, and stores runtime data under
`%LOCALAPPDATA%\PlanCommissionWorkbench\data`. It prompts for a credited OpenAI
API key when the key is missing. The key is used only for that desktop session
and is not embedded in the executable, committed to git, or written to disk.
Packaged startup logs are written to `server.log` and `server.err.log` in that
same data folder so server failures can be diagnosed without an IDE.

Build locally on Windows:

```powershell
.\scripts\build_windows.ps1
```

The build writes `artifacts\PlanCommissionWorkbench-windows.zip`. The GitHub
Actions workflow in `.github/workflows/windows-build.yml` builds the same
artifact on pushes to `main`, pull requests, and manual workflow dispatch.

## Tests

The test suite mocks Legistar, Docling, and OpenAI calls:

```bash
source .venv/bin/activate
pip install -e ".[test]"
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
```

## Python API

```python
from datetime import date
from plan_commission_workbench import PlanCommissionWorkbench

workbench = PlanCommissionWorkbench()
workbench.run_madison_range(date(2026, 1, 1), date(2026, 1, 31))
```
