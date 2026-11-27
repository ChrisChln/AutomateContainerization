from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import Workbook, load_workbook
from PySide6.QtWidgets import QApplication

import tt_auto_bag_scanner.application.app_controller as app_controller_module
from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import CarrierConfig
from tt_auto_bag_scanner.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _wait_for(app: QApplication, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert predicate(), "timed out waiting for the application operation"


def test_manual_sort_choice_flows_into_saved_import_snapshot(app, tmp_path) -> None:
    report = tmp_path / "ambiguous-carrier.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["tracking", "carrier", "status", "outbound_at"])
    outbound_at = (
        datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)
    ).replace(tzinfo=None)
    sheet.append(["TRACKING-SYNTHETIC-CHOICE", "CARRIER-X", "复核完成", outbound_at])
    workbook.save(report)

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "data")
    controller._show_error = lambda message: pytest.fail(message)
    now = datetime.now(UTC)
    configs = [
        CarrierConfig(
            carrier_key="CARRIER-X",
            display_name="Carrier X",
            aliases=(),
            sort_code=f"SORT-X-{suffix}",
            container_no=f"CONTAINER-X-{suffix}",
            enabled=True,
            version=1,
            updated_at_utc=now,
        )
        for suffix in ("A", "B")
    ]
    controller.repository.save_carrier_configs(configs, actor="operator-test")

    controller._build_preview(
        {
            "path": str(report),
            "sheet_name": "Sheet",
            "header_row": 1,
            "tracking": "tracking",
            "carrier": "carrier",
            "status": "status",
            "outbound_at": "outbound_at",
            "sort_code": "",
            "report_timezone": "America/New_York",
            "threshold_minutes": 30,
            "threshold_confirmed": True,
            "sort_code_overrides": {2: "SORT-X-B"},
        }
    )

    _wait_for(app, lambda: controller.current_task_id is not None)
    task_id = controller.current_task_id
    assert task_id is not None
    item = controller.repository.list_items(task_id)[0]
    assert item.sort_code == "SORT-X-B"
    snapshots = controller.repository.list_import_snapshots()
    assert snapshots[-1]["mapping"]["sort_code_overrides"] == [[2, "SORT-X-B"]]

    window.set_running(False)
    controller.close()
    window.close()


def test_preview_export_writes_candidates_and_exclusions(app, monkeypatch, tmp_path) -> None:
    report = tmp_path / "preview-export-source.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["tracking", "carrier", "status", "outbound_at"])
    outbound_at = (
        datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)
    ).replace(tzinfo=None)
    sheet.append(["TRACKING-SYNTHETIC-CANDIDATE", "CARRIER-X", "复核完成", outbound_at])
    sheet.append(["TRACKING-SYNTHETIC-EXCLUDED", "CARRIER-X", "待复核", outbound_at])
    workbook.save(report)

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "data")
    controller._show_error = lambda message: pytest.fail(message)
    config = CarrierConfig(
        carrier_key="CARRIER-X",
        display_name="Carrier X",
        aliases=(),
        sort_code="SORT-X-A",
        container_no="CONTAINER-X-A",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    preview = controller.import_service.build_preview(
        report,
        mapping=app_controller_module.ImportMapping(
            tracking_column="tracking",
            carrier_column="carrier",
            status_column="status",
            outbound_at_column="outbound_at",
            report_timezone="America/New_York",
            threshold_minutes=30,
        ),
        carrier_configs=(config,),
        as_of_utc=datetime.now(UTC),
        task_id="task-export-synthetic",
        sheet_name="Sheet",
        header_row=1,
    )
    output = tmp_path / "preview-export.xlsx"
    monkeypatch.setattr(
        app_controller_module.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(output), "Excel 工作簿 (*.xlsx)"),
    )

    controller._export_preview(preview)
    _wait_for(app, output.is_file)
    _wait_for(app, lambda: not controller._jobs)

    export = load_workbook(output, data_only=True)
    sheet = export.active
    assert sheet.max_row == 3
    assert sheet["A2"].value == "candidate"
    assert sheet["C2"].value == "TRACKING-SYNTHETIC-CANDIDATE"
    assert sheet["A3"].value == "excluded"
    assert sheet["C3"].value == "TRACKING-SYNTHETIC-EXCLUDED"
    assert sheet["I3"].value == "status_not_exact"
    assert '"tracking": "TRACKING-SYNTHETIC-EXCLUDED"' in sheet["J3"].value

    export.close()
    window.set_running(False)
    controller.close()
    window.close()
