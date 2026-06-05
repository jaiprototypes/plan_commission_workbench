from __future__ import annotations

import runpy
from pathlib import Path

from plan_commission_workbench.desktop_launcher import default_data_dir


def test_default_data_dir_uses_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    assert default_data_dir() == Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"


def test_launcher_file_imports_as_top_level_script() -> None:
    path = Path(__file__).resolve().parents[1] / "plan_commission_workbench" / "desktop_launcher.py"

    namespace = runpy.run_path(str(path))

    assert namespace["APP_NAME"] == "Plan Commission Workbench"
