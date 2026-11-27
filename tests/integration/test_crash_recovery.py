from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.application.scan_worker import ScanWorker
from tt_auto_bag_scanner.application.startup_recovery import recover_on_startup
from tt_auto_bag_scanner.application.task_service import TaskService
from tt_auto_bag_scanner.domain.models import (
    DeviceMode,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    ScanEvidence,
    ScanOutcome,
    ScanResult,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository


@pytest.mark.parametrize("crash_after_send", [False, True])
def test_process_crash_before_or_after_send_never_retries(tmp_path, crash_after_send) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "scanner.sqlite3"
    repository = TaskRepository(db_path)
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
        id="task-synthetic",
        name="Synthetic task",
        state=TaskState.RUNNING,
        item_ids=(item.id,),
        counts=TaskCounts.from_statuses([ItemStatus.IN_FLIGHT]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.SIMULATED,
    )
    repository.create_task(task, [item])
    check = PhysicalCheck(
        id="check-synthetic",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="operator-synthetic",
        checked_at_utc=now,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="evidence-synthetic",
    )
    repository.record_physical_check(check)
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )

    class SimulatedProcessCrash(BaseException):
        pass

    class CrashingController:
        device_mode = DeviceMode.SIMULATED

        def __init__(self):
            self.calls = 0

        def submit_once(self, current_item, current_session, current_check, attempt_id):
            assert repository.get_item(current_item.id).status is ItemStatus.IN_FLIGHT
            if crash_after_send:
                self.calls += 1
            raise SimulatedProcessCrash("synthetic process termination")

        def recover_duplicate(self, result, current_session):
            pytest.fail("duplicate recovery is not expected in this crash scenario")

    controller = CrashingController()
    worker = ScanWorker(repository, task.id, controller, lambda _: session)
    with pytest.raises(SimulatedProcessCrash):
        worker.run()

    assert repository.get_item(item.id).status is ItemStatus.IN_FLIGHT
    assert controller.calls == int(crash_after_send)
    attempt_id = repository.latest_attempt(item.id)["id"]

    repository.close()
    reopened = TaskRepository(db_path)
    recovered = recover_on_startup(reopened)
    service = TaskService(reopened)

    assert recovered.unknown_attempt_ids == (attempt_id,)
    assert reopened.get_item(item.id).status is ItemStatus.UNKNOWN
    assert reopened.get_task(task.id).state is TaskState.BLOCKED
    assert reopened.statistics(task.id)["attempts"] == 1
    with pytest.raises(RepositoryError, match="blocked|reconciliation"):
        service.start_task(task.id, controller, lambda _: session)
    assert controller.calls == int(crash_after_send)


def test_process_crash_during_duplicate_recovery_blocks_without_repeating_action(tmp_path) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "scanner.sqlite3"
    repository = TaskRepository(db_path)
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
        id="task-synthetic",
        name="Synthetic task",
        state=TaskState.RUNNING,
        item_ids=(item.id,),
        counts=TaskCounts.from_statuses([ItemStatus.WAIT_PHYSICAL]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.SIMULATED,
    )
    repository.create_task(task, [item])
    check = PhysicalCheck(
        id="check-synthetic",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="operator-synthetic",
        checked_at_utc=now,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="evidence-synthetic",
    )
    repository.record_physical_check(check)
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )

    class SimulatedProcessCrash(BaseException):
        pass

    class Controller:
        device_mode = DeviceMode.SIMULATED
        scan_calls = 0
        recovery_calls = 0

        def submit_once(self, current_item, current_session, current_check, attempt_id):
            self.scan_calls += 1
            return ScanResult(
                attempt_id=attempt_id,
                item_id=current_item.id,
                tracking=current_item.tracking,
                outcome=ScanOutcome.ALREADY_IN_BAG,
                evidence=ScanEvidence(
                    observed_at_utc=now,
                    container_no=current_session.container_no,
                    sort_code=current_session.sort_code,
                    logid="LOGID-SYNTHETIC",
                ),
            )

        def recover_duplicate(self, result, current_session):
            self.recovery_calls += 1
            raise SimulatedProcessCrash("synthetic termination during one recovery action")

    controller = Controller()
    worker = ScanWorker(repository, task.id, controller, lambda _: session)
    with pytest.raises(SimulatedProcessCrash):
        worker.run()

    attempt_id = repository.latest_attempt(item.id)["id"]
    assert controller.scan_calls == 1
    assert controller.recovery_calls == 1
    assert repository.get_attempt(attempt_id)["recovery_status"] == "in_flight"
    repository.close()

    reopened = TaskRepository(db_path)
    recovered = recover_on_startup(reopened)
    assert recovered.unknown_attempt_ids == ()
    assert reopened.get_attempt(attempt_id)["recovery_status"] == "unknown"
    assert reopened.has_unresolved_recovery(task.id)
    assert reopened.get_task(task.id).state is TaskState.BLOCKED
    with pytest.raises(RepositoryError, match="blocked|reconciliation"):
        TaskService(reopened).start_task(task.id, controller, lambda _: session)
    assert controller.scan_calls == 1
    assert controller.recovery_calls == 1


def test_restart_pauses_safely_when_crash_happens_before_attempt_creation(tmp_path) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "scanner.sqlite3"
    repository = TaskRepository(db_path)
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
        id="task-synthetic",
        name="Synthetic task",
        state=TaskState.RUNNING,
        item_ids=(item.id,),
        counts=TaskCounts.from_statuses([item.status]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.SIMULATED,
    )
    repository.create_task(task, [item])
    repository.record_physical_check(
        PhysicalCheck(
            id="check-synthetic",
            item_id=item.id,
            tracking=item.tracking,
            container_no=item.container_no,
            operator="operator-synthetic",
            checked_at_utc=now,
            method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
            evidence_ref="evidence-synthetic",
        )
    )
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )
    repository.record_session_verified(item.id, session)
    repository.close()

    reopened = TaskRepository(db_path)
    recovered = recover_on_startup(reopened)

    assert recovered.unknown_attempt_ids == ()
    assert reopened.statistics(task.id)["attempts"] == 0
    assert reopened.get_item(item.id).status is ItemStatus.READY
    assert reopened.get_task(task.id).state is TaskState.PAUSED
    assert reopened.events(task_id=task.id)[-1]["event_type"] == "startup_task_paused"
