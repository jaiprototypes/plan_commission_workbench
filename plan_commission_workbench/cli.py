from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from . import statuses
from .api import PlanCommissionWorkbench
from .settings import OpenAIKeyManager


def _parse_date(value: str) -> dt.date:
    """Purpose: parse CLI date strings into exact scrape bounds."""

    return dt.date.fromisoformat(value)


def main() -> int:
    """Purpose: expose run, serve, retry, and export commands."""

    parser = argparse.ArgumentParser(prog="pcw", description="Madison Plan Commission Workbench")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_parser = subcommands.add_parser("run", help="Run a scrape pipeline")
    run_subcommands = run_parser.add_subparsers(dest="source", required=True)
    madison = run_subcommands.add_parser("madison", help="Run Madison Plan Commission")
    madison.add_argument("--from", dest="date_from", type=_parse_date, required=True)
    madison.add_argument("--to", dest="date_to", type=_parse_date, required=True)
    madison.add_argument("--request-text", default=None)

    serve = subcommands.add_parser("serve", help="Start the FastAPI workbench server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)

    export = subcommands.add_parser("export", help="Export reviewed rows")
    export.add_argument("--status", default=statuses.ACCEPTED)
    export.add_argument("--output", type=Path, required=True)

    retry = subcommands.add_parser("retry", help="Retry a failed or incomplete run")
    retry.add_argument("--run-id", type=int, required=True)

    args = parser.parse_args()
    if args.command == "serve":
        return _serve(args.host, args.port)
    if args.command in {"run", "retry"}:
        OpenAIKeyManager().prompt_if_missing(required=True)
    workbench = PlanCommissionWorkbench()
    if args.command == "run" and args.source == "madison":
        result = workbench.run_madison_range(args.date_from, args.date_to, args.request_text)
    elif args.command == "export":
        result = workbench.export_rows(args.output, args.status)
    elif args.command == "retry":
        result = workbench.retry_run(args.run_id)
    else:
        parser.error("Unsupported command")
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


def _serve(host: str, port: int) -> int:
    """Purpose: run the local server without requiring a global uvicorn command."""

    import uvicorn

    OpenAIKeyManager().prompt_if_missing(required=False)
    uvicorn.run("plan_commission_workbench.server:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
