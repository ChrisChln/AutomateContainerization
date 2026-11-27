from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import Workbook
from PySide6.QtWidgets import QApplication

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    TaskState,
)
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    return instance if instance is not None else QApplication([])


def test_application_defaults_to_simulation_and_separate_local_databases(
    app: QApplication, tmp_path
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)

    assert controller.mode is DeviceMode.SIMULATED
    assert controller.repository.path == (tmp_path / "simulated.sqlite3").resolve()
    assert controller.repositories[DeviceMode.REAL].path == (
        tmp_path / "real.sqlite3"
    ).resolve()
    fake = controller.adapters[DeviceMode.SIMULATED]
    assert isinstance(fake, FakeDeviceAdapter)
    assert fake.adb_write_call_count == 0
    assert controller.repository.list_carrier_configs() == ()

    controller.close()
    window.close()


def test_import_defaults_are_saved_and_restored_for_the_next_report(
    app: QApplication, tmp_path
) -> None:
    report = tmp_path / "synthetic-import-defaults.xlsx"
    workbook = Workbook()
    workbook.active.title = "Summary"
    workbook.active.append(["Report summary"])
    outbound = workbook.create_sheet("Outbound")
    outbound.append(["Report title"])
    outbound.append(["tracking", "carrier", "status", "outbound_at", "sort_code"])
    outbound_at = (datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)).replace(
        tzinfo=None
    )
    outbound.append(
        [
            "TRACKING-SYNTHETIC-DEFAULTS",
            "CARRIER-SYNTHETIC",
            "复核完成",
            outbound_at,
            "SORT-SYNTHETIC",
        ]
    )
    workbook.save(report)

    data_dir = tmp_path / "data"
    window = MainWindow()
    controller = AppController(window, data_dir=data_dir, poll_interval_ms=50)
    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic Carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC-DEFAULTS",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    controller.repository.save_carrier_configs([config], actor="operator-test")
    window.set_report_path(str(report))
    controller._build_preview(
        {
            "path": str(report),
            "sheet_name": "Outbound",
            "header_row": 2,
            "tracking": "tracking",
            "carrier": "carrier",
            "status": "status",
            "outbound_at": "outbound_at",
            "sort_code": "sort_code",
            "report_timezone": "America/New_York",
            "threshold_minutes": 30,
            "threshold_confirmed": True,
        }
    )

    deadline = time.monotonic() + 5
    while controller.current_task_id is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert controller.current_task_id is not None

    settings = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    defaults = settings["import_defaults"]
    assert defaults == {
        "sheet_name": "Outbound",
        "header_row": 2,
        "columns": {
            "tracking": "tracking",
            "carrier": "carrier",
            "status": "status",
            "outbound_at": "outbound_at",
            "sort_code": "sort_code",
        },
        "report_timezone": "America/New_York",
        "threshold_minutes": 30,
    }

    controller.close()
    window.close()

    restored_window = MainWindow()
    restored_controller = AppController(
        restored_window,
        data_dir=data_dir,
        poll_interval_ms=50,
    )
    restored_window.set_report_path(str(report))
    result = restored_controller._inspect_report(
        str(report),
        restored_controller._import_defaults(),
    )
    restored_controller._report_headers_loaded(str(report), result)

    assert restored_window.sheet_combo.currentData() == "Outbound"
    assert restored_window.header_row_spin.value() == 2
    assert {
        key: combo.currentData() for key, combo in restored_window.column_combos.items()
    } == defaults["columns"]
    assert restored_window.timezone_combo.currentData() == "America/New_York"
    assert restored_window.threshold_spin.value() == 30
    assert restored_window.confirm_threshold_check.isChecked() is False

    restored_controller.close()
    restored_window.close()


def test_switching_mode_switches_task_store_without_mixing_history(
    app: QApplication, tmp_path
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)

    controller.set_mode(DeviceMode.REAL)

    assert controller.mode is DeviceMode.REAL
    assert controller.repository is controller.repositories[DeviceMode.REAL]
    assert controller.repository.path.name == "real.sqlite3"
    assert controller.adapters[DeviceMode.REAL] is not controller.adapters[DeviceMode.SIMULATED]
    controller.close()
    window.close()


def test_simulated_import_to_scan_workflow_never_calls_adb(tmp_path, app: QApplication) -> None:
    report = tmp_path / "synthetic-outbound.xlsx"
    workbook = Workbook()
    workbook.active.title = "Summary"
    sheet = workbook.create_sheet("Outbound")
    sheet.append(["Report title"])
    sheet.append(["tracking", "carrier", "status", "outbound_at", "sort_code"])
    outbound_time = (datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)).replace(
        tzinfo=None
    )
    sheet.append(
        [
            "TRACKING-SYNTHETIC-001",
            "CARRIER-SYNTHETIC",
            "复核完成",
            outbound_time,
            "SORT-SYNTHETIC",
        ]
    )
    workbook.save(report)

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "data", poll_interval_ms=50)
    controller._show_error = lambda message: pytest.fail(message)
    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic Carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC-001",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    controller.repository.save_carrier_configs([config], actor="operator-test")
    window.set_report_path(str(report))

    controller._build_preview(
        {
            "path": str(report),
            "sheet_name": "Outbound",
            "header_row": 2,
            "tracking": "tracking",
            "carrier": "carrier",
            "status": "status",
            "outbound_at": "outbound_at",
            "sort_code": "sort_code",
            "report_timezone": "America/New_York",
            "threshold_minutes": 30,
            "threshold_confirmed": True,
        }
    )

    def wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), "timed out waiting for the simulated workflow"

    wait_for(lambda: controller.current_task_id is not None)
    task_id = controller.current_task_id
    assert task_id is not None
    item = controller.repository.list_items(task_id)[0]
    assert item.status is ItemStatus.WAIT_PHYSICAL

    controller._record_physical_check(item.id, "operator-test")
    assert controller.repository.get_item(item.id).status is ItemStatus.READY
    controller._review_task()
    assert controller.repository.get_task(task_id).state is TaskState.READY
    assert controller._verified_session_valid()

    controller._start_task()
    wait_for(lambda: controller.repository.get_task(task_id).state is TaskState.COMPLETED)
    assert controller.repository.get_item(item.id).status is ItemStatus.SUCCESS
    assert controller.adapters[DeviceMode.SIMULATED].adb_write_call_count == 0
    window.set_running(False)
    controller.close()
    window.close()


def test_report_carriers_become_pending_rows_until_container_is_filled(
    app: QApplication, tmp_path
) -> None:
    report = tmp_path / "synthetic-pending-carriers.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["tracking", "carrier", "status", "outbound_at", "sort_code"])
    outbound_at = (datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)).replace(
        tzinfo=None
    )
    sheet.append(["TRACKING-SYN-P1", "CARRIER-SYN-P", "复核完成", outbound_at, "SORT-SYN-P"])
    workbook.save(report)

    data_dir = tmp_path / "data"
    window = MainWindow()
    controller = AppController(window, data_dir=data_dir, poll_interval_ms=50)
    window.set_report_path(str(report))
    controller._build_preview(
        {
            "path": str(report),
            "sheet_name": sheet.title,
            "header_row": 1,
            "tracking": "tracking",
            "carrier": "carrier",
            "status": "status",
            "outbound_at": "outbound_at",
            "sort_code": "sort_code",
            "report_timezone": "America/New_York",
            "threshold_minutes": 30,
            "threshold_confirmed": True,
        }
    )
    deadline = time.monotonic() + 5
    while window.carrier_table.rowCount() == 0 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert window.carrier_table.rowCount() == 1
    assert window.carrier_table.item(0, 0).text() == "CARRIER-SYN-P"
    assert window.carrier_table.item(0, 3).text() == "SORT-SYN-P"
    assert window.carrier_table.item(0, 5).text() == "待填大包号"
    assert controller.repository.list_carrier_configs() == ()
    settings = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["pending_carriers"] == [["CARRIER-SYN-P", "SORT-SYN-P"]]

    controller._save_carrier_config(
        {
            "carrier_key": "CARRIER-SYN-P",
            "display_name": "CARRIER-SYN-P",
            "aliases": (),
            "sort_code": "SORT-SYN-P",
            "container_no": "CONTAINER-SYN-P",
            "enabled": True,
        }
    )

    assert window.carrier_table.rowCount() == 1
    assert window.carrier_table.item(0, 4).text() == "CONTAINER-SYN-P"
    assert window.carrier_table.item(0, 5).text() == "启用"

    controller.close()
    window.close()
