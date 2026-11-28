from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    return instance if instance is not None else QApplication([])


def test_application_runs_real_device_mode_only(app: QApplication, tmp_path) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)

    assert controller.mode is DeviceMode.REAL
    assert controller.repository.path == (tmp_path / "real.sqlite3").resolve()
    assert set(controller.repositories) == {DeviceMode.REAL}
    assert not (tmp_path / "simulated.sqlite3").exists()
    assert not hasattr(controller, "set_mode")

    controller.close()
    window.close()


@pytest.mark.parametrize("scrcpy_location", ["app_data", "bundled", "missing"])
def test_connect_automatically_opens_read_only_preview(
    app, tmp_path, monkeypatch, fake_real_pda, scrcpy_location
) -> None:
    bundled_root = tmp_path / "_internal"
    monkeypatch.setattr("sys._MEIPASS", str(bundled_root), raising=False)
    search_root = bundled_root if scrcpy_location == "bundled" else tmp_path
    executable = search_root / "tools" / "scrcpy" / "scrcpy.exe"
    if scrcpy_location != "missing":
        executable.parent.mkdir(parents=True)
        executable.touch()
    monkeypatch.setattr("shutil.which", lambda command: None)
    started = []
    statuses = []
    monkeypatch.setattr(QProcess, "start", lambda self, *args: started.append(args))
    pda = fake_real_pda({"BAG-TEST": "SORT-TEST"})
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path, adapter_factory=pda.factory)
    monkeypatch.setattr(controller, "_set_status", statuses.append)
    try:
        pda.authorize_and_connect(app, controller)

        if scrcpy_location != "missing":
            assert started == [
                (str(executable), ["--serial", pda.serial, "--no-control", "--no-audio"])
            ]
        else:
            assert started == []
            assert "未找到 scrcpy" in statuses[-1]
        assert window.device_status_label.text() == "设备已连接"
    finally:
        controller.close()
        window.close()


def test_workspace_speed_persists_and_updates_active_worker(app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    received = []
    worker = SimpleNamespace(set_scan_speed=received.append)
    try:
        controller.current_task_id = "synthetic-task"
        monkeypatch.setattr(controller.task_service, "active_worker", lambda task_id: worker)
        window.workspace_speed_buttons["fast"].click()

        assert received == ["fast"]
        saved = json.loads(controller.settings_file.read_text(encoding="utf-8"))
        assert saved["scan_speed"] == "fast"
        assert window.settings_speed_buttons["fast"].isChecked()
    finally:
        controller.current_task_id = None
        controller.close()
        window.close()


def test_detects_adb_in_android_sdk_and_displays_path(app, tmp_path, monkeypatch) -> None:
    sdk = tmp_path / "sdk"
    adb = sdk / "platform-tools" / "adb.exe"
    adb.parent.mkdir(parents=True)
    adb.touch()
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setattr("shutil.which", lambda command: None)
    adapter_paths = []

    def adapter_factory(path: str) -> FakeDeviceAdapter:
        adapter_paths.append(path)
        return FakeDeviceAdapter()

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "app", adapter_factory=adapter_factory)
    try:
        assert adapter_paths == [str(adb)]
        assert window.adb_path_edit.placeholderText() == str(adb)
    finally:
        controller.close()
        window.close()


def test_poll_refreshes_new_activity_without_refreshing_queue(app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    repository = controller.repository
    now = datetime.now(UTC)
    task = TaskSnapshot(
        id="test-task",
        name="Test task",
        state=TaskState.PAUSED,
        item_ids=(),
        counts=TaskCounts.from_statuses([]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.REAL,
    )
    try:
        repository.create_task(task, [])
        controller.current_task_id = task.id
        controller._refresh_visible_data(force=True)
        baseline = repository.get_task(task.id).updated_at_utc

        def unexpected_queue_refresh(*args):
            pytest.fail("a log-only change should not refresh the queue")

        monkeypatch.setattr(window, "set_queue_items", unexpected_queue_refresh)
        repository.append_log("info", "synthetic log-only update", task_id=task.id)
        assert repository.get_task(task.id).updated_at_utc == baseline
        controller._poll()
        assert "synthetic log-only update" in window.activity_console.toPlainText()
    finally:
        controller.close()
        window.close()


def test_activity_times_use_selected_report_timezone(app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    monkeypatch.setattr(controller.repository, "logs", lambda **kwargs: (
        {"at_utc": "2026-11-27T02:42:02+00:00", "level": "error", "message": "scan failed"},
    ))
    monkeypatch.setattr(controller.repository, "events", lambda **kwargs: ())
    try:
        window.timezone_combo.setCurrentIndex(window.timezone_combo.findData("America/Los_Angeles"))
        assert controller._task_activity_rows("test-task") == [("18:42:02", "ERROR", "scan failed")]
        window.timezone_combo.setCurrentIndex(window.timezone_combo.findData("America/New_York"))
        assert controller._task_activity_rows("test-task") == [("21:42:02", "ERROR", "scan failed")]
    finally:
        controller.close()
        window.close()


def test_activity_formats_scan_results_with_carrier_and_tracking(
    app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    monkeypatch.setattr(controller.repository, "events", lambda **kwargs: tuple(
        {
            "at_utc": "2026-11-27T02:42:02+00:00",
            "event_type": "attempt_finished",
            "item_id": item_id,
            "detail": {"outcome": outcome},
        }
        for item_id, outcome in (("first", "success"), ("second", "already_in_bag"))
    ))
    items = {
        "first": SimpleNamespace(carrier_key="Test Carrier", tracking="000123"),
        "second": SimpleNamespace(carrier_key="Test Carrier", tracking="000124"),
    }
    try:
        rows = controller._task_activity_rows("test-task", items_by_id=items)
        assert [message for _, _, message in rows] == [
            "扫描成功：Test Carrier · 000123",
            "重复扫描，已跳过：Test Carrier · 000124",
        ]
    finally:
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
    controller.real_mode_authorized = True
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

    restored_controller.close()
    restored_window.close()


@pytest.mark.parametrize("resume", [False, True])
def test_import_to_scan_workflow_with_fake_pda_never_calls_adb(
    tmp_path, app: QApplication, fake_real_pda, resume
) -> None:
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

    pda = fake_real_pda({"CONTAINER-SYNTHETIC-001": "SORT-SYNTHETIC"}, initial_page="home")
    window = MainWindow()
    controller = AppController(
        window, data_dir=tmp_path / "data", poll_interval_ms=50, adapter_factory=pda.factory
    )
    controller._show_error = lambda message: pytest.fail(message)
    pda.authorize_and_connect(app, controller)
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
        }
    )

    def wait_for(predicate, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert predicate(), "timed out waiting for the fake PDA workflow"

    wait_for(lambda: controller.current_task_id is not None)
    task_id = controller.current_task_id
    assert task_id is not None
    item = controller.repository.list_items(task_id)[0]
    assert item.status is ItemStatus.WAIT_PHYSICAL
    assert window.workspace_task_state_label.text() == "任务状态：草稿"

    controller._record_physical_check(item.id, "operator-test")
    assert controller.repository.get_item(item.id).status is ItemStatus.READY
    controller._review_task()
    assert controller.repository.get_task(task_id).state is TaskState.READY
    assert window.workspace_task_state_label.text() == "任务状态：待启动"
    if resume:
        for state in (TaskState.RUNNING, TaskState.PAUSED):
            controller.repository.transition_task(
                task_id, state, actor="operator-test", reason="test resume"
            )
        controller._poll()

    assert controller.verified_session is None
    assert window.start_button.isEnabled()
    assert window.start_button.text() == ("继续任务" if resume else "开始实扫")

    controller._start_task()
    wait_for(lambda: controller.repository.get_task(task_id).state is TaskState.COMPLETED)
    wait_for(lambda: window.workspace_task_state_label.text() == "任务状态：已完成")
    assert controller.repository.get_item(item.id).status is ItemStatus.SUCCESS
    assert controller.verified_session is not None
    assert controller.verified_session.container_no == config.container_no
    assert window.bag_session_label.text() == "WMS 会话有效"
    assert pda.adapter.adb_write_call_count == 0
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
    controller.real_mode_authorized = True
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


def _task_waiting_for_physical_checks(app: QApplication, tmp_path, *, skip: bool):
    report = tmp_path / "synthetic-skip.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["tracking", "carrier", "status", "outbound_at", "sort_code"])
    outbound_at = (datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=2)).replace(
        tzinfo=None
    )
    for tracking in ("TRACKING-SYN-S1", "TRACKING-SYN-S2"):
        sheet.append([tracking, "CARRIER-SYN-S", "复核完成", outbound_at, "SORT-SYN-S"])
    workbook.save(report)
    data_dir = tmp_path / "data"
    window = MainWindow()
    controller = AppController(window, data_dir=data_dir, poll_interval_ms=50)
    controller.real_mode_authorized = True
    controller.settings["skip_physical_check"] = skip
    controller.repository.save_carrier_configs(
        [
            CarrierConfig(
                carrier_key="CARRIER-SYN-S",
                display_name="CARRIER-SYN-S",
                aliases=(),
                sort_code="SORT-SYN-S",
                container_no="CONTAINER-SYN-S",
                enabled=True,
                version=1,
                updated_at_utc=datetime.now(UTC),
            )
        ],
        actor="operator-test",
    )
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
        }
    )
    deadline = time.monotonic() + 5
    while controller.current_task_id is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert controller.current_task_id is not None
    return window, controller


def test_without_skip_setting_physical_checks_still_gate_start(
    app: QApplication, tmp_path
) -> None:
    window, controller = _task_waiting_for_physical_checks(app, tmp_path, skip=False)

    controller._refresh_gates()
    controller._review_task()

    assert not window._physical_checks_complete
    items = controller.repository.list_items(controller.current_task_id)
    assert {item.status for item in items} == {ItemStatus.WAIT_PHYSICAL}
    assert controller.repository.get_task(controller.current_task_id).state is TaskState.DRAFT
    controller.close()
    window.close()


def test_skip_setting_alone_allows_start_and_records_authorized_skip(
    app: QApplication, tmp_path
) -> None:
    window, controller = _task_waiting_for_physical_checks(app, tmp_path, skip=True)
    controller.settings["operator_id"] = "operator-syn-skip"
    controller._show_error = lambda message: pytest.fail(message)

    controller._refresh_gates()
    assert window._physical_checks_complete
    assert window._task_ready

    assert controller._apply_authorized_skips()

    items = controller.repository.list_items(controller.current_task_id)
    assert {item.status for item in items} == {ItemStatus.READY}
    checks = [controller.repository.get_physical_check(item.id) for item in items]
    assert {check.method.value for check in checks} == {"authorized_skip"}
    assert {check.operator for check in checks} == {"operator-syn-skip"}
    assert controller.repository.get_task(controller.current_task_id).state is TaskState.READY
    controller.close()
    window.close()


def test_skip_requires_an_operator_name(app: QApplication, tmp_path) -> None:
    window, controller = _task_waiting_for_physical_checks(app, tmp_path, skip=True)
    controller.settings["operator_id"] = ""
    errors: list[str] = []
    controller._show_error = errors.append

    assert not controller._apply_authorized_skips()
    assert errors == ["跳过实物核验需要先在设置中填写操作员"]

    items = controller.repository.list_items(controller.current_task_id)
    assert {item.status for item in items} == {ItemStatus.WAIT_PHYSICAL}
    controller.close()
    window.close()


def test_workspace_confirms_unknown_item_with_one_click(
    app: QApplication, tmp_path, monkeypatch
) -> None:
    window, controller = _task_waiting_for_physical_checks(app, tmp_path, skip=True)
    try:
        controller.settings["operator_id"] = "operator-syn-confirm"
        assert controller._apply_authorized_skips()
        repo = controller.repository
        task_id = controller.current_task_id
        item, other = repo.list_items(task_id)
        repo.transition_task(task_id, TaskState.RUNNING, actor="test", reason="test start")
        session = VerifiedSession(
            serial="SYN-DEVICE",
            warehouse="SYN-WAREHOUSE",
            container_no=item.container_no,
            sort_code=item.sort_code,
            session_id="syn-session",
            verified_at_utc=datetime.now(UTC),
        )
        attempt_id = repo.begin_attempt(item.id, session, repo.get_physical_check(item.id))
        repo.mark_attempt_unknown(attempt_id, "synthetic timeout", actor="test")
        repo.transition_task(task_id, TaskState.PAUSED, actor="test", reason="test pause")
        controller._refresh_visible_data(force=True)
        window.workspace_table.selectRow(0)

        def fail_on_dialog(*args, **kwargs):
            pytest.fail("one-click confirmation must not open the reconciliation form")

        monkeypatch.setattr(window, "open_reconciliation_dialog", fail_on_dialog)
        monkeypatch.setattr(controller, "_show_error", pytest.fail)
        assert window.review_result_button.isEnabled()
        window.review_result_button.click()

        assert repo.get_item(item.id).status is ItemStatus.SUCCESS
        assert repo.get_item(other.id).status is ItemStatus.READY
        assert window.task_progress_label.text() == "已确认 1 / 2"
        assert window.task_attempted_label.text() == "已尝试 1 / 2"
        assert window.review_result_button.text() == "确认通过"
        assert not window.review_result_button.isEnabled()
        event = repo.events(task_id=task_id, item_id=item.id)[-1]
        assert event["actor"] == "operator-syn-confirm"
        assert event["detail"]["reason"] == "人工确认通过"
    finally:
        controller.close()
        window.close()


def test_sort_mode_setting_selects_controller_and_invalidates_on_change(
    app: QApplication, tmp_path
) -> None:
    from tt_auto_bag_scanner.infrastructure.wms_controller import (
        MixedSortController,
        WmsController,
    )

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "data", poll_interval_ms=50)
    adapter = FakeDeviceAdapter()

    single = controller._new_wms_controller(adapter, warehouse="SYN-WAREHOUSE")
    assert type(single) is WmsController

    controller.wms_controller = single
    window.sort_mode_buttons["mixed"].click()

    assert controller.wms_controller is None
    assert controller._controller_invalidated
    mixed = controller._new_wms_controller(adapter, warehouse="SYN-WAREHOUSE")
    assert isinstance(mixed, MixedSortController)


def test_poll_refreshes_progress_when_item_counts_change_mid_run(
    app: QApplication, tmp_path
) -> None:
    from tt_auto_bag_scanner.domain.models import PhysicalCheck, PhysicalCheckMethod

    window, controller = _task_waiting_for_physical_checks(app, tmp_path, skip=False)
    controller._poll()
    item = controller.repository.list_items(controller.current_task_id)[0]
    task_before = controller.repository.get_task(controller.current_task_id)

    # An item changes status without a task state transition (as during a scan).
    controller.repository.record_physical_check(
        PhysicalCheck(
            id="check-syn-progress",
            item_id=item.id,
            tracking=item.tracking,
            container_no=item.container_no,
            operator="operator-test",
            checked_at_utc=datetime.now(UTC),
            method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
            evidence_ref="synthetic-evidence",
        ),
        actor="operator-test",
    )
    assert (
        controller.repository.get_task(controller.current_task_id).updated_at_utc
        == task_before.updated_at_utc
    )
    refreshed: list[bool] = []
    original = controller._refresh_visible_data
    controller._refresh_visible_data = lambda force=False: (  # type: ignore[method-assign]
        refreshed.append(force),
        original(force=force),
    )
    controller._poll()

    assert refreshed


def _launch_with_settings(app, tmp_path, monkeypatch, pda, settings):
    monkeypatch.setattr(QProcess, "start", lambda self, *args: None)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path, adapter_factory=pda.factory)
    deadline = time.monotonic() + 3
    while not controller._connected and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return window, controller


def test_auto_connect_on_launch_connects_saved_device(
    app, tmp_path, monkeypatch, fake_real_pda
) -> None:
    pda = fake_real_pda({"BAG-TEST": "SORT-TEST"})
    window, controller = _launch_with_settings(
        app,
        tmp_path,
        monkeypatch,
        pda,
        {"auto_connect_device": True, "operator_id": "operator-test", "warehouse": pda.warehouse},
    )
    try:
        assert controller._connected
        assert window.auto_connect_check.isChecked()
        saved = json.loads(controller.settings_file.read_text(encoding="utf-8"))
        assert saved["last_device_serial"] == pda.serial
    finally:
        controller.close()
        window.close()


def test_auto_connect_is_off_by_default(app, tmp_path, monkeypatch, fake_real_pda) -> None:
    pda = fake_real_pda({"BAG-TEST": "SORT-TEST"})
    window, controller = _launch_with_settings(
        app, tmp_path, monkeypatch, pda, {"operator_id": "operator-test"}
    )
    try:
        assert not controller._connected
        assert not window.auto_connect_check.isChecked()
    finally:
        controller.close()
        window.close()
