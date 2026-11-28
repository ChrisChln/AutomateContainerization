from __future__ import annotations

from datetime import UTC, datetime
from threading import Event

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

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
from tt_auto_bag_scanner.ui.main_window import MainWindow


class BlockingContainerController:
    def __init__(
        self,
        adapter: object,
        started: Event,
        release: Event,
        *,
        authorized: bool,
        warehouse: str,
    ) -> None:
        self.adapter = adapter
        self.started = started
        self.release = release
        self.real_submission_authorized = authorized
        self.warehouse = warehouse
        self.invalidated = False

    def verify_current_or_open_bag(
        self, config: CarrierConfig, *, unresolved_submissions: int | None = None
    ) -> VerifiedSession:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release container verification")
        return VerifiedSession(
            serial="SYN-PDA",
            warehouse=self.warehouse,
            container_no=config.container_no,
            sort_code=config.sort_code,
            session_id="SESSION-SYNTHETIC",
            verified_at_utc=datetime.now(UTC),
        )

    def invalidate_session(self) -> None:
        self.invalidated = True


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def test_stale_container_verification_callback_does_not_replace_current_controller(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, window, blocking_controllers, started, release = _start_verification(
        app,
        tmp_path,
        monkeypatch,
    )
    try:
        assert started.wait(timeout=2)
        current_controller = object()
        controller.wms_controller = current_controller  # type: ignore[assignment]

        _finish_jobs(app, controller, release)

        assert controller.wms_controller is current_controller
        assert controller.verified_session is None
        assert blocking_controllers[0].invalidated is True
    finally:
        controller.close()
        _close_window(app, window)


def test_device_config_and_settings_actions_are_rejected_while_verifying(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, window, _blocking_controllers, started, release = _start_verification(
        app,
        tmp_path,
        monkeypatch,
    )
    errors: list[str] = []
    controller._show_error = errors.append  # type: ignore[method-assign]
    monkeypatch.setattr(controller, "_poll", lambda: None)
    monkeypatch.setattr(controller.window, "set_running", lambda _running: None)
    refresh_calls: list[bool] = []
    monkeypatch.setattr(
        controller.device_manager,
        "refresh",
        lambda: refresh_calls.append(True) or controller.device_manager.devices,
    )
    original_configs = controller.repository.list_carrier_configs()
    original_settings = dict(controller.settings)
    item, session = _create_ready_item(controller)
    controller.current_task_id = item.task_id
    controller.verified_session = session
    controller.wms_controller = controller.wms_controller or _blocking_controllers[0]
    task_starts: list[bool] = []
    monkeypatch.setattr(
        controller.task_service,
        "start_task",
        lambda *_args, **_kwargs: task_starts.append(True),
    )
    try:
        assert started.wait(timeout=2)

        controller._review_task()
        controller._start_task()
        controller._refresh_devices()
        controller._connect_device("SYN-PDA")
        controller._verify_container("CARRIER-SYNTHETIC")
        controller._save_carrier_config(
            {
                "carrier_key": "OTHER-CARRIER",
                "display_name": "Other",
                "sort_code": "OTHER-SORT",
                "container_no": "OTHER-BAG",
                "enabled": True,
            }
        )
        controller._save_window_settings({"warehouse": "CHANGED-WAREHOUSE"})

        assert refresh_calls == []
        assert len(controller._jobs) == 1
        assert controller.repository.list_carrier_configs() == original_configs
        assert controller.settings == original_settings
        assert task_starts == []
        assert len(errors) == 7
    finally:
        _finish(app, controller, window, release)


def _start_verification(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "real", poll_interval_ms=60_000)
    controller._poll_timer.stop()
    controller.settings["warehouse"] = "SYN-WAREHOUSE"
    controller.real_mode_authorized = True
    controller._connected = True
    controller._controller_invalidated = True

    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    controller.repository.save_carrier_configs([config], actor="operator-synthetic")
    window.carrier_key_edit.setText(config.carrier_key)
    window.sort_code_edit.setText(config.sort_code)
    window.container_edit.setText(config.container_no)

    started = Event()
    release = Event()
    blocking_controllers: list[BlockingContainerController] = []

    def create_controller(adapter, *, warehouse: str, real_submission_authorized: bool = False):
        result = BlockingContainerController(
            adapter,
            started,
            release,
            authorized=real_submission_authorized,
            warehouse=warehouse,
        )
        blocking_controllers.append(result)
        return result

    monkeypatch.setattr(controller, "_new_wms_controller", create_controller)
    controller._verify_container(config.carrier_key)
    return controller, window, blocking_controllers, started, release


def _create_ready_item(controller: AppController) -> tuple[QueueItem, VerifiedSession]:
    now = datetime.now(UTC)
    item = QueueItem(
        id="item-synthetic",
        task_id="task-synthetic",
        tracking="TRACK-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        outbound_at_utc=now,
        source_row_numbers=(2,),
    )
    task = TaskSnapshot(
        id=item.task_id,
        name="Synthetic task",
        state=TaskState.READY,
        item_ids=(item.id,),
        counts=TaskCounts.from_statuses([ItemStatus.WAIT_PHYSICAL]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.REAL,
    )
    controller.repository.create_task(task, [item])
    check = controller.physical_check_service.record_manual(
        item,
        operator="operator-synthetic",
        evidence_ref="attestation-synthetic",
        checked_at_utc=now,
    )
    controller.repository.record_physical_check(check, actor="operator-synthetic")
    session = VerifiedSession(
        serial="SYN-PDA",
        warehouse="SYN-WAREHOUSE",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-READY-SYNTHETIC",
        verified_at_utc=now,
    )
    return item, session


def _finish(
    app: QApplication,
    controller: AppController,
    window: MainWindow,
    release: Event,
) -> None:
    _finish_jobs(app, controller, release)
    controller.close()
    _close_window(app, window)


def _finish_jobs(app: QApplication, controller: AppController, release: Event) -> None:
    release.set()
    for job in tuple(controller._jobs):
        assert job.wait(3_000)
    app.processEvents()


def _close_window(app: QApplication, window: MainWindow) -> None:
    window.close()
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_new_controller_does_not_switch_from_stale_app_session(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "real", poll_interval_ms=60_000)
    controller._poll_timer.stop()
    controller.settings["warehouse"] = "SYN-WAREHOUSE"
    controller.real_mode_authorized = True
    controller._connected = True
    controller._controller_invalidated = True
    controller.verified_session = VerifiedSession(
        serial="SYN-PDA",
        warehouse="SYN-WAREHOUSE",
        container_no="OLD-BAG",
        sort_code="OLD-SORT",
        session_id="SESSION-OLD",
        verified_at_utc=datetime.now(UTC),
    )
    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    controller.repository.save_carrier_configs([config], actor="operator-synthetic")
    window.carrier_key_edit.setText(config.carrier_key)
    window.sort_code_edit.setText(config.sort_code)
    window.container_edit.setText(config.container_no)
    started = Event()
    release = Event()
    release.set()
    switch_calls: list[bool] = []

    def create_controller(adapter, *, warehouse: str, real_submission_authorized: bool = False):
        result = BlockingContainerController(
            adapter, started, release, authorized=real_submission_authorized, warehouse=warehouse
        )
        result.switch_bag = lambda *_a, **_k: switch_calls.append(True)  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr(controller, "_new_wms_controller", create_controller)
    try:
        controller._verify_container(config.carrier_key)
        assert started.wait(timeout=2)
        assert switch_calls == []
    finally:
        _finish(app, controller, window, release)


def test_controller_without_session_verifies_instead_of_switching(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "real", poll_interval_ms=60_000)
    controller._poll_timer.stop()
    controller.settings["warehouse"] = "SYN-WAREHOUSE"
    controller.real_mode_authorized = True
    controller._connected = True
    controller._controller_invalidated = False
    started = Event()
    release = Event()
    release.set()
    switch_calls: list[bool] = []
    wms = BlockingContainerController(
        controller.adapter, started, release, authorized=True, warehouse="SYN-WAREHOUSE"
    )
    wms.active_session = None  # type: ignore[attr-defined]
    wms.switch_bag = lambda *_a, **_k: switch_calls.append(True)  # type: ignore[attr-defined]
    controller.wms_controller = wms  # type: ignore[assignment]
    controller.verified_session = VerifiedSession(
        serial="SYN-PDA",
        warehouse="SYN-WAREHOUSE",
        container_no="OLD-BAG",
        sort_code="OLD-SORT",
        session_id="SESSION-OLD",
        verified_at_utc=datetime.now(UTC),
    )
    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    controller.repository.save_carrier_configs([config], actor="operator-synthetic")
    window.carrier_key_edit.setText(config.carrier_key)
    window.sort_code_edit.setText(config.sort_code)
    window.container_edit.setText(config.container_no)
    try:
        controller._verify_container(config.carrier_key)
        assert started.wait(timeout=2)
        assert switch_calls == []
    finally:
        _finish(app, controller, window, release)
