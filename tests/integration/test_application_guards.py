from __future__ import annotations

import os
import time
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    QueueItem,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.infrastructure.wms_controller import WmsController
from tt_auto_bag_scanner.ui.main_window import MainWindow


def _task(task_id: str, state: TaskState, mode: DeviceMode) -> TaskSnapshot:
    now = datetime.now(UTC)
    return TaskSnapshot(
        id=task_id,
        name="Synthetic task",
        state=state,
        item_ids=(),
        counts=TaskCounts.from_statuses(()),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=mode,
    )


def _wait_for(app: QApplication, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert predicate(), "timed out waiting for the application operation"


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


@pytest.mark.parametrize("connection_fails", [False, True])
def test_connect_button_grants_authorization_only_on_success(
    app, tmp_path, fake_real_pda, monkeypatch, connection_fails
) -> None:
    pda = fake_real_pda({"CONTAINER-SYNTHETIC": "SORT-SYNTHETIC"})
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path, adapter_factory=pda.factory)
    controller.settings["operator_id"] = "operator-test"
    errors = []
    monkeypatch.setattr(controller, "_show_error", errors.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: pytest.fail("Unexpected confirmation"))
    window.set_devices(controller.device_manager.refresh())

    def fail_to_launch(_package: str) -> None:
        raise RuntimeError("Synthetic launch failure")

    if connection_fails:
        monkeypatch.setattr(controller.device_manager, "launch_app", fail_to_launch)
    try:
        window.device_connect_button.click()
        _wait_for(app, lambda: not controller._has_pending_device_jobs())

        assert controller.real_mode_authorized is not connection_fails
        assert bool(errors) is connection_fails
        assert not hasattr(window, "device_authorize_check")
        assert not window.start_button.isEnabled()
    finally:
        controller.close()
        window.close()


def test_container_verification_navigates_from_pda_home(app, tmp_path, fake_real_pda) -> None:
    pda = fake_real_pda({"CONTAINER-SYNTHETIC-001": "SORT-SYNTHETIC"}, initial_page="home")
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path, adapter_factory=pda.factory)
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
    window.sort_code_edit.setText(config.sort_code)
    window.container_edit.setText(config.container_no)

    controller._verify_container(config.carrier_key)
    _wait_for(app, lambda: controller.verified_session is not None)

    assert controller.verified_session.container_no == config.container_no
    assert controller.verified_session.sort_code == config.sort_code
    adapter = controller.adapters[DeviceMode.REAL]
    assert sum(action[0] == "tap" for action in adapter.actions) >= 3

    controller.close()
    window.close()


def test_changed_carrier_container_revokes_old_physical_evidence(app, tmp_path) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    controller._show_error = lambda message: pytest.fail(message)
    now = datetime.now(UTC)
    original = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic Carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-OLD",
        enabled=True,
        version=1,
        updated_at_utc=now,
    )
    controller.repository.save_carrier_configs([original], actor="operator-test")
    task = _task("task-config-change", TaskState.DRAFT, DeviceMode.REAL)
    item = QueueItem(
        id="item-config-change",
        task_id=task.id,
        tracking="TRACKING-SYNTHETIC",
        carrier_key=original.carrier_key,
        sort_code=original.sort_code,
        container_no=original.container_no,
        outbound_at_utc=now,
        source_row_numbers=(2,),
    )
    controller.repository.create_task(task, [item])
    check = controller.physical_check_service.record_manual(
        item,
        operator="operator-test",
        evidence_ref="physical-check-test",
        checked_at_utc=now,
    )
    controller.repository.record_physical_check(check, actor="operator-test")

    controller._save_carrier_config(
        {
            "carrier_key": original.carrier_key,
            "display_name": original.display_name,
            "aliases": (),
            "sort_code": original.sort_code,
            "container_no": "CONTAINER-NEW",
            "enabled": True,
        }
    )

    saved_check = controller.repository.get_physical_check(item.id)
    assert saved_check is not None and saved_check.revoked_at_utc is not None
    assert controller.repository.get_item(item.id).status is ItemStatus.WAIT_PHYSICAL
    with pytest.raises(ValueError, match="no longer matches"):
        controller._config_for_item(item)

    controller.close()
    window.close()


def test_blocked_task_without_unresolved_attempts_returns_to_paused(app, tmp_path) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    task = _task("task-safe-block", TaskState.BLOCKED, DeviceMode.REAL)
    controller.repository.create_task(task, [])
    controller.current_task_id = task.id
    controller._last_refreshed_task = None

    controller._poll()

    assert controller.repository.get_task(task.id).state is TaskState.PAUSED
    controller.close()
    window.close()


def test_stopped_real_task_invalidates_authorization_and_device_session(app, tmp_path) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    task = _task("task-stopped-real", TaskState.STOPPED, DeviceMode.REAL)
    controller.repository.create_task(task, [])
    controller.current_task_id = task.id
    controller.real_mode_authorized = True
    controller.wms_controller = WmsController(
        FakeDeviceAdapter(),
        warehouse="SYNTHETIC-WAREHOUSE",
        real_submission_authorized=True,
    )
    controller.verified_session = VerifiedSession(
        serial="SYNTHETIC-DEVICE",
        warehouse="SYNTHETIC-WAREHOUSE",
        container_no="CONTAINER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        session_id="session-synthetic",
        verified_at_utc=datetime.now(UTC),
    )
    controller._last_refreshed_task = None

    controller._poll()

    assert controller.real_mode_authorized is False
    assert controller.verified_session is None
    assert controller._controller_invalidated is True
    assert controller.wms_controller.real_submission_authorized is False
    assert controller.wms_controller._blocked is True

    controller.close()
    window.close()


def test_cancel_selected_unsubmitted_item_through_main_window(app, tmp_path, monkeypatch) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    controller._show_error = lambda message: pytest.fail(message)
    now = datetime.now(UTC)
    task = _task("task-cancel-unsubmitted", TaskState.DRAFT, DeviceMode.REAL)
    item = QueueItem(
        id="item-cancel-unsubmitted",
        task_id=task.id,
        tracking="TRACKING-CANCEL-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        outbound_at_utc=now,
        source_row_numbers=(2,),
    )
    controller.repository.create_task(task, [item])
    check = controller.physical_check_service.record_manual(
        item,
        operator="operator-test",
        evidence_ref="physical-check-synthetic",
        checked_at_utc=now,
    )
    controller.repository.record_physical_check(check, actor="operator-test")
    controller.current_task_id = task.id
    controller._last_refreshed_task = None
    controller._refresh_visible_data(force=True)
    window.workspace_table.selectRow(0)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.cancel_item_button.click()

    assert controller.repository.get_item(item.id).status is ItemStatus.CANCELLED
    saved_check = controller.repository.get_physical_check(item.id)
    assert saved_check is not None and saved_check.revoked_at_utc is not None
    event = controller.repository.events(task_id=task.id, item_id=item.id)[-1]
    assert event["event_type"] == "item_cancelled"
    assert event["actor"] == "local-operator"

    controller.close()
    window.close()


def test_device_job_disables_pda_verify_button_until_finished(app, tmp_path) -> None:
    import threading

    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    release = threading.Event()
    done: list[object] = []

    controller._run_job(release.wait, done.append, device_job=True)

    assert not window.container_verify_button.isEnabled()
    assert window.container_verify_button.text() != "PDA 验证"
    release.set()
    _wait_for(app, lambda: done and not controller._has_pending_device_jobs())
    assert window.container_verify_button.isEnabled()
    assert window.container_verify_button.text() == "PDA 验证"


def test_carrier_rows_report_container_verification_result(
    app, tmp_path, fake_real_pda
) -> None:
    pda = fake_real_pda({"CONTAINER-SYNTHETIC-001": "SORT-SYNTHETIC"}, initial_page="home")
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path, adapter_factory=pda.factory)
    errors: list[str] = []
    controller._show_error = errors.append
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
    window.sort_code_edit.setText(config.sort_code)
    window.container_edit.setText(config.container_no)
    assert controller._carrier_rows()[0]["verification"] == "unverified"

    controller._verify_container(config.carrier_key)
    _wait_for(app, lambda: controller.verified_session is not None)
    assert window.carrier_table.item(0, 6).text() == "已验证"

    def fail() -> None:
        raise RuntimeError("synthetic verification failure")

    controller.wms_controller.verify_current_or_open_bag = lambda _config: fail()
    controller._run_job_original = controller._run_job

    def failing_run_job(operation, on_success, **kwargs):
        return controller._run_job_original(fail, on_success, **kwargs)

    controller._run_job = failing_run_job
    controller._verify_container(config.carrier_key)
    _wait_for(app, lambda: bool(errors))
    _wait_for(app, lambda: window.carrier_table.item(0, 6).text() == "验证失败")

    controller.close()
    window.close()
