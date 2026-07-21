"""Subprocess entry point for Madison scrape runs started by the web UI."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback

from . import statuses
from .api import PlanCommissionWorkbench
from .models import RunRequest


def main(argv: list[str] | None = None) -> int:
    """Purpose: execute one already-created run in a dedicated process."""

    parser = argparse.ArgumentParser(prog="pcw-run-worker")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--date-from", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--request-text", default=None)
    args = parser.parse_args(argv)
    workbench = PlanCommissionWorkbench()
    request = RunRequest(args.date_from, args.date_to, args.request_text)
    try:
        result = workbench.execute_madison_run(args.run_id, request, register_worker=False)
    except Exception as exc:
        workbench.store.fail_run_from_exception(args.run_id, statuses.FAILED_AGENDA_LLM, exc)
        traceback.print_exc(file=sys.stderr)
        return 1
    print(json.dumps(result or {"run_id": args.run_id}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
