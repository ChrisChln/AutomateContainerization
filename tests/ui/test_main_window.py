from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from tt_auto_bag_scanner.domain.models import ItemStatus, QueueItem
from tt_auto_bag_scanner.ui import main_window
from tt_auto_bag_scanner.ui.main_window import (
    MainWindow,
    PhysicalCheckDialog,
    PreflightDialog,
    ReconciliationDialog,
)


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    yield instance
    for widget in instance.topLevelWidgets():
        if widget.parent() is None:
            if isinstance(widget, MainWindow):
                widget.set_running(False)
            widget.close()
            widget.deleteLater()
    instance.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    instance.processEvents()


def test_main_window_exposes_scanner_workspaces_without_explanation_copy(app: QApplication) -> None:
    window = MainWindow()

    assert window.page_names == ("扫描工作区", "导入报表", "容器设置", "任务记录", "设置")
    assert window.status_mode_label.text() == "实机"
    assert not hasattr(window, "device_mode_combo")
    assert window.start_button.text() == "开始实扫"
    assert window.start_button.isEnabled() is False
    assert all(
        label.wordWrap() is False for label in window.findChildren(type(window.status_mode_label))
    )
    window.close()


def test_activity_console_colors_log_keywords(app: QApplication) -> None:
    window = MainWindow()
    try:
        entries = [
            ("EVENT", "扫描成功：Carrier · 000123", "扫描成功", "#86efac"),
            ("EVENT", "重复扫描，已跳过：Carrier · 000124", "重复扫描，已跳过", "#93c5fd"),
            ("EVENT", "attempt_finished · operator · unknown", "unknown", "#fcd34d"),
            ("WARNING", "Check the device", "[WARNING]", "#fcd34d"),
            ("ERROR", "Scan failed", "[ERROR]", "#fca5a5"),
            ("EVENT", "session_verified", "", ""),
        ]
        window.set_activity_rows(("18:42:02", level, message) for level, message, _, _ in entries)
        app.processEvents()

        document = window.activity_console.document()
        for index, (level, message, keyword, color) in enumerate(entries):
            block = document.findBlockByNumber(index)
            line = f"18:42:02  [{level}]  {message}"
            assert block.text() == line
            actual = [
                (span.start, span.length, span.format.foreground().color().name())
                for span in block.layout().formats()
            ]
            expected = [(line.index(keyword), len(keyword), color)] if keyword else []
            assert actual == expected
    finally:
        window.close()


def test_activity_console_shows_logs_and_follows_only_at_bottom(app: QApplication) -> None:
    window = MainWindow()
    window.show()
    app.processEvents()
    rows = [("18:42:02", "EVENT", f"scan {index}") for index in range(40)]
    window.set_activity_rows(rows)
    console = window.activity_console
    scroll = console.verticalScrollBar()

    assert console.isReadOnly()
    assert console.toPlainText().splitlines()[0] == "18:42:02  [EVENT]  scan 0"
    assert scroll.value() == scroll.maximum() > 0
    scroll.setValue(0)
    window.set_activity_rows([*rows, ("18:43:00", "ERROR", "scan failed")])
    assert scroll.value() == 0
    assert console.toPlainText().endswith("18:43:00  [ERROR]  scan failed")
    assert window.activity_log_label.text() == "41 条"
    window.set_activity_rows(())
    assert console.toPlainText() == ""
    window.close()


def test_start_gate_requires_device_task_and_physical_evidence(app: QApplication) -> None:
    window = MainWindow()

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=False,
        unresolved_count=0,
        real_mode_authorized=True,
    )
    assert window.start_button.isEnabled() is False

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=True,
    )
    assert window.start_button.isEnabled() is True
    window.close()


def test_real_mode_requires_explicit_authorization(app: QApplication) -> None:
    window = MainWindow()

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=False,
    )
    assert window.start_button.isEnabled() is False

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=True,
    )
    assert window.start_button.isEnabled() is True
    window.close()


def test_preview_button_uses_displayed_threshold(app: QApplication) -> None:
    window = MainWindow()
    window.set_report_path("synthetic.xlsx")
    window.set_import_sheets(("Outbound",), selected="Outbound")
    columns = {key: key for key in ("tracking", "carrier", "status", "outbound_at")}
    window.set_import_headers(columns, selected_columns=columns)
    window.set_timezones(("UTC",))
    window.apply_import_defaults({"report_timezone": "UTC", "threshold_minutes": 42})
    requests = []
    window.preview_requested.connect(requests.append)

    window.preview_button.click()

    assert [request["threshold_minutes"] for request in requests] == [42]
    assert not hasattr(window, "confirm_threshold_check")
    window.close()


def test_navigation_and_action_buttons_emit_commands(app: QApplication) -> None:
    window = MainWindow()
    calls: list[str] = []
    window.open_report_requested.connect(lambda: calls.append("open_report"))
    window.pause_requested.connect(lambda: calls.append("pause"))

    window.open_report_button.click()
    window.set_running(True)
    window.pause_button.click()
    window.navigate_to("任务记录")

    assert calls == ["open_report", "pause"]
    assert window.current_page_name == "任务记录"
    window.set_running(False)
    window.close()


def test_cancel_button_is_gated_to_unsubmitted_items_and_emits_after_confirmation(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = QueueItem(
        id="item-cancel",
        task_id="task-cancel",
        tracking="TRACKING-CANCEL-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
        source_row_numbers=(2,),
    )
    window = MainWindow()
    calls: list[str] = []
    window.cancel_item_requested.connect(calls.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.set_queue_items([item])
    window.workspace_table.selectRow(0)
    assert window.cancel_item_button.isEnabled() is True

    window.set_running(True)
    assert window.cancel_item_button.isEnabled() is False
    window.set_running(False)
    window.set_queue_items(
        [replace(item, status=ItemStatus.READY, physical_check_id="check-synthetic")]
    )
    window.workspace_table.selectRow(0)
    assert window.cancel_item_button.isEnabled() is True

    window.set_queue_items([replace(item, status=ItemStatus.UNKNOWN)])
    window.workspace_table.selectRow(0)
    assert window.cancel_item_button.isEnabled() is False

    window.set_queue_items([item])
    window.workspace_table.selectRow(0)
    window.cancel_item_button.click()

    assert calls == [item.id]
    window.close()


def test_workspace_shows_repeat_scan_result(app: QApplication) -> None:
    item = QueueItem(
        id="item-repeat",
        task_id="task-repeat",
        tracking="SYN-TRACKING",
        carrier_key="SYN-CARRIER",
        sort_code="SYN-SORT",
        container_no="SYN-CONTAINER",
        outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
        source_row_numbers=(2,),
        status=ItemStatus.REJECTED,
        last_attempt_error="重复扫描，已跳过",
    )
    window = MainWindow()
    try:
        window.set_queue_items([item])
        window.workspace_table.selectRow(0)
        assert window.workspace_table.item(0, 5).text() == "重复扫描"
        assert window.selected_result_label.text() == "结果：重复扫描"
    finally:
        window.close()


def test_import_controls_stay_locked_when_scan_state_changes(app: QApplication) -> None:
    window = MainWindow()

    window.set_import_busy(True)
    window.set_running(True)
    assert window.open_report_button.isEnabled() is False
    assert window.preview_button.isEnabled() is False
    assert window.restore_button.isEnabled() is False
    assert window.pause_button.isEnabled() is True

    window.set_running(False)
    assert window.open_report_button.isEnabled() is False
    assert window.preview_button.isEnabled() is False
    assert window.carrier_save_button.isEnabled() is True

    window.set_import_busy(False)
    assert window.open_report_button.isEnabled() is True
    assert window.preview_button.isEnabled() is True
    window.close()


def test_physical_confirmation_requires_operator_and_checkbox(app: QApplication) -> None:
    dialog = PhysicalCheckDialog(
        QueueItem(
            id="item-synthetic",
            task_id="task-synthetic",
            tracking="TRACKING-SYNTHETIC",
            carrier_key="CARRIER-SYNTHETIC",
            sort_code="SORT-SYNTHETIC",
            container_no="CONTAINER-SYNTHETIC",
            outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
            source_row_numbers=(2,),
        )
    )

    assert dialog.confirm_button.isEnabled() is False
    dialog.operator_edit.setText("operator-test")
    assert dialog.confirm_button.isEnabled() is False
    dialog.confirm_check.setChecked(True)
    assert dialog.confirm_button.isEnabled() is True
    assert dialog.operator == "operator-test"
    dialog.close()


def test_preflight_keeps_live_scan_blocked_without_authorization(app: QApplication) -> None:
    dialog = PreflightDialog(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=False,
        eligible_count=4,
        verified_count=4,
    )

    assert dialog.start_live_button.isEnabled() is False
    dialog.close()


def test_preflight_blocks_live_scan_when_unknown_results_remain(app: QApplication) -> None:
    dialog = PreflightDialog(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=1,
        real_mode_authorized=True,
        eligible_count=4,
        verified_count=4,
    )

    assert dialog.start_live_button.isEnabled() is False
    dialog.close()


def test_preflight_enables_live_scan_when_all_checks_pass(app: QApplication) -> None:
    dialog = PreflightDialog(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=True,
        eligible_count=1,
        verified_count=1,
    )

    assert dialog.start_live_button.isEnabled() is True
    dialog.close()


def test_reconciliation_requires_operator_reason_and_evidence(app: QApplication) -> None:
    item = QueueItem(
        id="item-unknown",
        task_id="task-unknown",
        tracking="TRACKING-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
        source_row_numbers=(2,),
        status=ItemStatus.UNKNOWN,
    )
    dialog = ReconciliationDialog(item, {})

    assert dialog.confirm_button.isEnabled() is False
    dialog.operator_edit.setText("operator-test")
    dialog.reason_edit.setText("WMS 已核对")
    dialog.evidence_edit.setText("LOG-SYNTHETIC-1")
    assert dialog.confirm_button.isEnabled() is True

    ready_index = dialog.outcome_combo.findData(ItemStatus.READY.value)
    dialog.outcome_combo.setCurrentIndex(ready_index)
    assert dialog.confirm_button.isEnabled() is False
    dialog.physical_check.setChecked(True)
    assert dialog.confirm_button.isEnabled() is True
    dialog.close()


def test_scan_workspace_shows_age_before_result(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 26, 12, 42, 30, tzinfo=UTC)

    monkeypatch.setattr(main_window, "datetime", FixedDatetime)
    window = MainWindow()
    item = QueueItem(
        id="item-age",
        task_id="task-age",
        tracking="TRACKING-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
        source_row_numbers=(2,),
    )

    window.set_queue_items([item])

    table = window.workspace_table
    assert [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())] == [
        "追踪号", "承运商", "出库时间", "实物核验", "年龄", "结果"
    ]
    assert [table.item(0, i).text() for i in range(3, 6)] == ["待核验", "42 分", "待核验"]
    assert window.queue_table.item(0, 4).text() == "42 分"
    window.close()


def test_scan_queue_tables_page_large_candidate_lists(app: QApplication) -> None:
    window = MainWindow()
    items = [
        QueueItem(
            id=f"item-{index}",
            task_id="task-large",
            tracking=f"TRACKING-{index:05d}",
            carrier_key="CARRIER-SYNTHETIC",
            sort_code="SORT-SYNTHETIC",
            container_no="CONTAINER-SYNTHETIC",
            outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
            source_row_numbers=(index + 2,),
        )
        for index in range(401)
    ]

    window.set_queue_items(items)

    assert window.queue_table.rowCount() == 200
    assert window.workspace_table.rowCount() == 200
    window.preview_next_button.click()
    assert window.queue_table.rowCount() == 200
    assert window._table_global_index(window.queue_table, 0) == 200
    window.close()
    app.processEvents()


def test_report_timezone_defaults_to_new_york_without_saved_choice(app: QApplication) -> None:
    window = MainWindow()
    window.set_timezones(["America/Chicago", "America/New_York", "UTC"])

    window.apply_import_defaults({})
    assert window.timezone_combo.currentData() == "America/New_York"

    window.apply_import_defaults({"report_timezone": "UTC"})
    assert window.timezone_combo.currentData() == "UTC"


def test_pending_carrier_row_prefills_editor_and_waits_for_container(app: QApplication) -> None:
    window = MainWindow()
    window.set_carrier_rows(
        [
            {
                "carrier_key": "Carrier A",
                "display_name": "Carrier A",
                "aliases": (),
                "sort_code": "SORT-A1",
                "container_no": "",
                "enabled": False,
                "pending": True,
            }
        ]
    )

    assert window.carrier_table.item(0, 5).text() == "待填大包号"
    window.carrier_table.selectRow(0)
    assert window.carrier_key_edit.text() == "Carrier A"
    assert window.sort_code_edit.text() == "SORT-A1"
    assert window.container_edit.text() == ""
    assert window.carrier_enabled_check.isChecked()


def test_preview_shows_awaiting_container_rows_without_physical_check(
    app: QApplication, tmp_path
) -> None:
    from openpyxl import Workbook

    from tt_auto_bag_scanner.application.import_service import ImportService
    from tt_auto_bag_scanner.domain.models import ImportMapping

    path = tmp_path / "awaiting-ui.xlsx"
    workbook = Workbook()
    workbook.active.append(["Tracking", "Carrier", "Status", "Outbound At", "Sort"])
    workbook.active.append(
        ["SYN-WAIT-UI", "Carrier A", "复核完成", datetime(2026, 11, 26, 17, 0), "SORT-A1"]
    )
    workbook.save(path)
    preview = ImportService().build_preview(
        path,
        ImportMapping("Tracking", "Carrier", "Status", "Outbound At", "UTC", 30, "Sort"),
        [],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-awaiting-ui",
    )
    window = MainWindow()

    window.set_import_preview(preview)

    assert window.queue_table.rowCount() == 1
    assert window.queue_table.item(0, 0).text() == "待填大包号"
    assert window.queue_table.item(0, 1).text() == "SYN-WAIT-UI"
    assert window.queue_table.item(0, 5).text() == ""
    window.queue_table.selectRow(0)
    assert not window.physical_check_button.isEnabled()
    assert window.workspace_table.rowCount() == 0


def test_skip_physical_check_setting_defaults_off_and_is_saved(app: QApplication) -> None:
    window = MainWindow()
    saved: list[dict] = []
    window.settings_save_requested.connect(saved.append)

    window.set_settings({})
    assert not window.skip_physical_check.isChecked()
    window.skip_physical_check.setChecked(True)
    window.save_settings_button.click()

    assert saved[-1]["skip_physical_check"] is True


def test_workspace_sort_mode_buttons_default_single_and_request_mixed(
    app: QApplication,
) -> None:
    window = MainWindow()
    requested: list[str] = []
    window.sort_mode_requested.connect(requested.append)

    window.set_settings({})
    assert window.sort_mode_buttons["single"].isChecked()
    assert window.sort_mode_buttons["mixed"].text() == "混合排序"
    window.sort_mode_buttons["mixed"].click()

    assert requested == ["mixed"]
    window.set_settings({"sort_mode": "mixed"})
    assert window.sort_mode_buttons["mixed"].isChecked()


def test_scan_speed_selectors_stay_synchronized_during_scan(app: QApplication) -> None:
    window = MainWindow()
    window.set_settings({})
    window.scan_speed_requested.connect(window.set_scan_speed)

    assert window.scan_interval_min.value() == 5
    assert window.scan_interval_max.value() == 30
    assert window.workspace_speed_buttons["normal"].isChecked()
    window.set_running(True)
    window.workspace_speed_buttons["fast"].click()
    assert window.settings_speed_buttons["fast"].isChecked()
    assert not window.workspace_speed_buttons["normal"].isChecked()
    window.settings_speed_buttons["none"].click()
    assert window.workspace_speed_buttons["none"].isChecked()
    assert not window.settings_speed_buttons["fast"].isChecked()
    window.set_running(False)


def test_carrier_table_shows_pda_verification_state(app: QApplication) -> None:
    window = MainWindow()
    base = {"display_name": "C", "aliases": (), "container_no": "BAG-1", "enabled": True}
    window.set_carrier_rows(
        [
            {**base, "carrier_key": "A", "sort_code": "S-A", "verification": "verified"},
            {**base, "carrier_key": "B", "sort_code": "S-B", "verification": "failed"},
            {**base, "carrier_key": "C", "sort_code": "S-C"},
        ]
    )

    assert window.carrier_table.horizontalHeaderItem(6).text() == "验证"
    assert window.carrier_table.item(0, 6).text() == "已验证"
    assert window.carrier_table.item(1, 6).text() == "验证失败"
    assert window.carrier_table.item(2, 6).text() == "未验证"
    window.carrier_table.selectRow(0)
    assert window.carrier_enabled_check.isChecked()


def test_workspace_filters_by_carrier_and_result_and_reports_visible_items(
    app: QApplication,
) -> None:
    def make(index: int, carrier: str, status: ItemStatus) -> QueueItem:
        return QueueItem(
            id=f"item-filter-{index}",
            task_id="task-filter",
            tracking=f"TRACKING-FILTER-{index}",
            carrier_key=carrier,
            sort_code="SORT-SYNTHETIC",
            container_no="CONTAINER-SYNTHETIC",
            outbound_at_utc=datetime(2026, 11, 26, 12, tzinfo=UTC),
            source_row_numbers=(index + 2,),
            status=status,
        )

    window = MainWindow()
    emitted: list[list[str]] = []
    window.visible_queue_items_changed.connect(emitted.append)
    window.set_queue_items(
        [
            make(0, "CARRIER-A", ItemStatus.READY),
            make(1, "CARRIER-B", ItemStatus.READY),
            make(2, "CARRIER-B", ItemStatus.REJECTED),
        ]
    )
    assert window.workspace_table.rowCount() == 3

    window.workspace_carrier_filter.setCurrentIndex(
        window.workspace_carrier_filter.findData("CARRIER-B")
    )
    assert window.workspace_table.rowCount() == 2
    window.workspace_status_filter.setCurrentIndex(
        window.workspace_status_filter.findData("rejected")
    )
    assert window.workspace_table.rowCount() == 1
    assert window.workspace_table.item(0, 0).text() == "TRACKING-FILTER-2"
    assert emitted[-1] == ["item-filter-2"]
    assert window.visible_workspace_item_ids() == ["item-filter-2"]
