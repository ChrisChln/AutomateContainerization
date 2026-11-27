from datetime import UTC, datetime

import pytest

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


@pytest.mark.parametrize(
    ("task_mode", "controller_mode"),
    [
        (DeviceMode.SIMULATED, DeviceMode.REAL),
        (DeviceMode.REAL, DeviceMode.SIMULATED),
        (DeviceMode.SIMULATED, None),
    ],
)
def test_start_task_rejects_missing_or_mismatched_controller_mode_before_start(
    tmp_path, task_mode: DeviceMode, controller_mode: DeviceMode | None
) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    repository = TaskRepository(tmp_path / "scanner.sqlite3")
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
    repository.create_task(
        TaskSnapshot(
            id=item.task_id,
            name="Synthetic task",
            state=TaskState.READY,
            item_ids=(item.id,),
            counts=TaskCounts.from_statuses([item.status]),
            created_at_utc=now,
            updated_at_utc=now,
            device_mode=task_mode,
            real_mode_authorized=task_mode is DeviceMode.REAL,
        ),
        [item],
    )
    physical_check = PhysicalCheck(
        id="check-synthetic",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="operator-synthetic",
        checked_at_utc=now,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="evidence-synthetic",
    )
    repository.record_physical_check(physical_check)
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )

    class Controller:
        def __init__(self) -> None:
            self.calls = 0
            if controller_mode is not None:
                self.device_mode = controller_mode

        def submit_once(self, *_args):
            self.calls += 1
            raise RuntimeError("submission must not run with a mismatched mode")

        def recover_duplicate(self, *_args):
            pytest.fail("duplicate recovery is not expected")

    controller = Controller()
    service = TaskService(repository)
    initial_events = repository.events(task_id=item.task_id)
    worker = None
    error = None
    try:
        worker = service.start_task(
            item.task_id,
            controller,
            lambda _: session,
            real_mode_authorized=task_mode is DeviceMode.REAL,
        )
    except RepositoryError as exc:
        error = exc
    finally:
        if worker is not None:
            worker.join(timeout=5)

    assert error is not None
    assert "mode" in str(error).lower()
    assert repository.get_task(item.task_id).state is TaskState.READY
    assert repository.events(task_id=item.task_id) == initial_events
    assert service.active_worker(item.task_id) is None
    assert service._workers == {}
    assert controller.calls == 0


def test_manual_unknown_reconciliation_requires_actor_reason_and_fresh_physical_check(
    tmp_path,
) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    repo = TaskRepository(tmp_path / "scanner.sqlite3")
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
        state=TaskState.READY,
        item_ids=(item.id,),
        counts=TaskCounts.from_statuses([item.status]),
        created_at_utc=now,
        updated_at_utc=now,
        device_mode=DeviceMode.SIMULATED,
    )
    repo.create_task(task, [item])
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
    repo.record_physical_check(check)
    repo.transition_task(
        task.id, TaskState.RUNNING, actor="operator-synthetic", reason="test setup"
    )
    attempt_id = repo.begin_attempt(item.id, _session(now), check)
    repo.mark_attempt_unknown(attempt_id, "synthetic process interruption")
    service = TaskService(repo)

    with pytest.raises(RepositoryError, match="reason"):
        service.reconcile_unknown(item.id, ItemStatus.READY, actor="operator-synthetic", reason=" ")
    with pytest.raises(RepositoryError, match="physical"):
        service.reconcile_unknown(
            item.id,
            ItemStatus.READY,
            actor="operator-synthetic",
            reason="confirmed not submitted",
        )

    renewed_check = PhysicalCheck(
        id="check-renewed",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="operator-synthetic",
        checked_at_utc=now,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="evidence-renewed",
    )
    service.reconcile_unknown(
        item.id,
        ItemStatus.READY,
        actor="operator-synthetic",
        reason="verified no WMS submission and parcel remains present",
        physical_check=renewed_check,
    )

    assert repo.get_item(item.id).status is ItemStatus.READY
    assert repo.get_physical_check(item.id).id == renewed_check.id
    event_types = [event["event_type"] for event in repo.events(task_id=task.id)]
    assert "item_reconciled" in event_types


def test_task_service_exposes_manual_duplicate_reconciliation(tmp_path) -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    repository = TaskRepository(tmp_path / "scanner.sqlite3")
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
    repository.create_task(
        TaskSnapshot(
            id=item.task_id,
            name="Synthetic task",
            state=TaskState.READY,
            item_ids=(item.id,),
            counts=TaskCounts.from_statuses([item.status]),
            created_at_utc=now,
            updated_at_utc=now,
            device_mode=DeviceMode.SIMULATED,
        ),
        [item],
    )
    check = PhysicalCheck(
        id="check-synthetic",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="operator-synthetic",
        checked_at_utc=now,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="physical-check-synthetic",
    )
    repository.record_physical_check(check)
    repository.transition_task(
        item.task_id,
        TaskState.RUNNING,
        actor="operator-synthetic",
        reason="test start",
    )
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )
    attempt_id = repository.begin_attempt(item.id, session, check)
    repository.finish_attempt(
        attempt_id,
        ScanResult(
            attempt_id=attempt_id,
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.ALREADY_IN_BAG,
            evidence=ScanEvidence(
                observed_at_utc=now,
                container_no=item.container_no,
                sort_code=item.sort_code,
                logid="LOGID-SYNTHETIC",
            ),
        ),
    )
    repository.transition_task(
        item.task_id,
        TaskState.BLOCKED,
        actor="scanner",
        reason="duplicate recovery pending",
    )

    task_service = TaskService(repository)
    result = task_service.reconcile_duplicate_recovery(
        attempt_id,
        tracking=item.tracking,
        container_no=item.container_no,
        logid="LOGID-SYNTHETIC",
        actor="operator-synthetic",
        reason="checked the WMS bag record",
        evidence_ref="wms-record:synthetic-check",
    )

    assert result is None
    assert repository.get_task(item.task_id).state is TaskState.PAUSED


def _session(now):
    from tt_auto_bag_scanner.domain.models import VerifiedSession

    return VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=now,
    )
