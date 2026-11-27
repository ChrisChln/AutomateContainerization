from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from tt_auto_bag_scanner.domain.models import DeviceMode, ItemStatus, QueueItem
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
    assert window.status_mode_label.text() == "模拟 · 不提交 WMS"
    assert window.start_button.isEnabled() is False
    assert all(
        label.wordWrap() is False for label in window.findChildren(type(window.status_mode_label))
    )
    window.close()


def test_start_gate_requires_device_task_and_physical_evidence(app: QApplication) -> None:
    window = MainWindow()

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=False,
        unresolved_count=0,
        mode=DeviceMode.SIMULATED,
    )
    assert window.start_button.isEnabled() is False

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        mode=DeviceMode.SIMULATED,
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
        mode=DeviceMode.REAL,
        real_mode_authorized=False,
    )
    assert window.start_button.isEnabled() is False

    window.set_start_conditions(
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        mode=DeviceMode.REAL,
        real_mode_authorized=True,
    )
    assert window.start_button.isEnabled() is True
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
        mode=DeviceMode.SIMULATED,
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=False,
        eligible_count=4,
        verified_count=4,
    )

    assert dialog.start_simulation_button.isEnabled() is True
    assert dialog.start_live_button.isEnabled() is False
    dialog.close()


def test_preflight_blocks_simulation_when_unknown_results_remain(app: QApplication) -> None:
    dialog = PreflightDialog(
        mode=DeviceMode.SIMULATED,
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=1,
        real_mode_authorized=True,
        eligible_count=4,
        verified_count=4,
    )

    assert dialog.start_simulation_button.isEnabled() is False
    assert dialog.start_live_button.isEnabled() is False
    dialog.close()


def test_preflight_only_enables_the_currently_selected_mode(app: QApplication) -> None:
    dialog = PreflightDialog(
        mode=DeviceMode.REAL,
        device_ready=True,
        task_ready=True,
        physical_checks_complete=True,
        unresolved_count=0,
        real_mode_authorized=True,
        eligible_count=1,
        verified_count=1,
    )

    assert dialog.start_live_button.isEnabled() is True
    assert dialog.start_simulation_button.isEnabled() is False
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
