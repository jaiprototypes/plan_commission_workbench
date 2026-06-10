from __future__ import annotations

import runpy
from pathlib import Path

from plan_commission_workbench.desktop_launcher import (
    SMOKE_TEST_TEXT,
    default_data_dir,
    desktop_log_paths,
    recent_error_summary,
    smoke_test_pdf_bytes,
)


def test_default_data_dir_uses_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    assert default_data_dir() == Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"


def test_desktop_logs_use_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    log_path, error_path = desktop_log_paths()

    data_dir = Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"
    assert log_path == data_dir / "server.log"
    assert error_path == data_dir / "server.err.log"


def test_recent_error_summary_tails_file(tmp_path) -> None:
    error_path = tmp_path / "server.err.log"
    error_path.write_text("\n".join(f"line {number}" for number in range(20)), encoding="utf-8")

    assert recent_error_summary(error_path, line_count=3) == "line 17\nline 18\nline 19"


def test_smoke_test_pdf_bytes_are_valid_pdf_shaped() -> None:
    payload = smoke_test_pdf_bytes()

    assert payload.startswith(b"%PDF-")
    assert b"%%EOF" in payload[-32:]
    assert SMOKE_TEST_TEXT.encode("ascii") in payload


def test_launcher_file_imports_as_top_level_script() -> None:
    path = Path(__file__).resolve().parents[1] / "plan_commission_workbench" / "desktop_launcher.py"

    namespace = runpy.run_path(str(path))

    assert namespace["APP_NAME"] == "Plan Commission Workbench"


def test_windows_build_explicitly_bundles_server_module() -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"
    script = script_path.read_text(encoding="utf-8")

    assert '--hidden-import "plan_commission_workbench.docling_worker"' in script
    assert '--hidden-import "plan_commission_workbench.server"' in script
    assert '--collect-all "docling_parse"' in script
    assert '--collect-all "pypdfium2_raw"' in script
    assert "--self-test-docling" in script
