from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication
from test_device_job_isolation import _close_window, _create_ready_item

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import TaskState
from tt_auto_bag_scanner.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def _controller(tmp_path) -> tuple[AppController, MainWindow]:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path / "real", poll_interval_ms=60_000)
    controller._poll_timer.stop()
    return controller, window


def test_clear_task_stops_and_unloads_but_keeps_records(app: QApplication, tmp_path) -> None:
    controller, window = _controller(tmp_path)
    try:
        item, _session = _create_ready_item(controller)
        controller.current_task_id = item.task_id

        controller._clear_task()

        assert controller.current_task_id is None
        assert controller.current_preview is None
        assert controller.repository.get_task(item.task_id).state is TaskState.STOPPED
        assert [i.id for i in controller.repository.list_items(item.task_id)] == [item.id]
        assert window.queue_table.rowCount() == 0
        assert not window.clear_task_button.isEnabled()
    finally:
        controller.close()
        _close_window(app, window)


def test_clear_task_refuses_with_unresolved_submissions(
    app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, window = _controller(tmp_path)
    errors: list[str] = []
    controller._show_error = errors.append  # type: ignore[method-assign]
    try:
        item, _session = _create_ready_item(controller)
        controller.current_task_id = item.task_id
        monkeypatch.setattr(
            controller.repository, "has_blocking_unresolved", lambda _task_id: True
        )

        controller._clear_task()

        assert controller.current_task_id == item.task_id
        assert controller.repository.get_task(item.task_id).state is TaskState.READY
        assert errors
    finally:
        controller.close()
        _close_window(app, window)


def test_cleared_task_is_not_reloaded_on_restart(app: QApplication, tmp_path) -> None:
    controller, window = _controller(tmp_path)
    item, _session = _create_ready_item(controller)
    controller.current_task_id = item.task_id
    controller._clear_task()
    controller.close()
    _close_window(app, window)

    restarted, window2 = _controller(tmp_path)
    try:
        assert restarted.current_task_id is None
    finally:
        restarted.close()
        _close_window(app, window2)


class _ReverifyController:
    active_session = None

    def __init__(self) -> None:
        self.verified: list[tuple[str, int | None]] = []
        self.switched = False

    def verify_current_or_open_bag(self, config, *, unresolved_submissions=None):
        from datetime import UTC, datetime

        from tt_auto_bag_scanner.domain.models import VerifiedSession

        self.verified.append((config.container_no, unresolved_submissions))
        return VerifiedSession(
            serial="SYN-PDA",
            warehouse="SYN-WAREHOUSE",
            container_no=config.container_no,
            sort_code=config.sort_code,
            session_id="SESSION-REVERIFIED",
            verified_at_utc=datetime.now(UTC),
        )

    def switch_bag(self, *_args, **_kwargs):
        self.switched = True
        raise AssertionError("switch_bag needs a controller session")


def test_session_provider_reverifies_after_unknown_dropped_the_session(
    app: QApplication, tmp_path
) -> None:
    from datetime import UTC, datetime

    from tt_auto_bag_scanner.domain.models import CarrierConfig

    controller, window = _controller(tmp_path)
    try:
        item, session = _create_ready_item(controller)
        controller.settings["warehouse"] = "SYN-WAREHOUSE"
        controller.repository.save_carrier_configs(
            [
                CarrierConfig(
                    carrier_key=item.carrier_key,
                    display_name=item.carrier_key,
                    aliases=(),
                    sort_code=item.sort_code,
                    container_no=item.container_no,
                    enabled=True,
                    version=1,
                    updated_at_utc=datetime.now(UTC),
                )
            ],
            actor="operator-synthetic",
        )
        controller.verified_session = session
        wms = _ReverifyController()

        provided = controller._provide_session(wms, item.task_id, item)

        assert provided.session_id == "SESSION-REVERIFIED"
        assert wms.verified == [(item.container_no, 0)]
        assert not wms.switched
        assert controller.verified_session is provided
    finally:
        controller.close()
        _close_window(app, window)


def test_blocked_task_with_only_unknown_items_returns_to_paused(
    app: QApplication, tmp_path
) -> None:
    controller, window = _controller(tmp_path)
    try:
        item, session = _create_ready_item(controller)
        repository = controller.repository
        repository.transition_task(item.task_id, TaskState.RUNNING, actor="op", reason="test")
        attempt = repository.begin_attempt(
            item.id, session, repository.get_physical_check(item.id), actor="op"
        )
        repository.mark_attempt_unknown(attempt, "synthetic ambiguous", actor="op")
        assert repository.get_task(item.task_id).state is TaskState.BLOCKED
        controller.current_task_id = item.task_id

        controller._poll()

        assert repository.get_task(item.task_id).state is TaskState.PAUSED
    finally:
        controller.close()
        _close_window(app, window)


def test_unknown_items_do_not_count_against_start_preflight(
    app: QApplication, tmp_path
) -> None:
    controller, window = _controller(tmp_path)
    try:
        item, session = _create_ready_item(controller)
        repository = controller.repository
        repository.transition_task(item.task_id, TaskState.RUNNING, actor="op", reason="test")
        attempt = repository.begin_attempt(
            item.id, session, repository.get_physical_check(item.id), actor="op"
        )
        repository.mark_attempt_unknown(attempt, "synthetic ambiguous", actor="op")
        controller.current_task_id = item.task_id

        assert controller._unresolved_count() == 0
    finally:
        controller.close()
        _close_window(app, window)


def test_clear_task_allowed_with_only_unknown_items(app: QApplication, tmp_path) -> None:
    controller, window = _controller(tmp_path)
    errors: list[str] = []
    controller._show_error = errors.append  # type: ignore[method-assign]
    try:
        item, session = _create_ready_item(controller)
        repository = controller.repository
        repository.transition_task(item.task_id, TaskState.RUNNING, actor="op", reason="test")
        attempt = repository.begin_attempt(
            item.id, session, repository.get_physical_check(item.id), actor="op"
        )
        repository.mark_attempt_unknown(attempt, "synthetic ambiguous", actor="op")
        controller.current_task_id = item.task_id
        controller._poll()

        controller._clear_task()

        assert not errors
        assert controller.current_task_id is None
        assert repository.get_item(item.id).status.value == "unknown"
    finally:
        controller.close()
        _close_window(app, window)
