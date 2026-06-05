from __future__ import annotations

from pathlib import Path

from plan_commission_workbench.desktop_launcher import default_data_dir


def test_default_data_dir_uses_local_app_data(monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\AppData\Local")

    assert default_data_dir() == Path(r"C:\Users\Tester\AppData\Local") / "PlanCommissionWorkbench" / "data"
