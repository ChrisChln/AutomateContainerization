import threading
from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.application.scan_worker import ScanWorker
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

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class InspectingController:
    device_mode = DeviceMode.SIMULATED

    def __init__(self, repository, outcome=ScanOutcome.SUCCESS, exception=None, after_submit=None):
        self.repository = repository
        self.outcome = outcome
        self.exception = exception
        self.after_submit = after_submit
        self.calls = 0
        self.status_seen_at_submit = None

    def submit_once(self, item, session, physical_check, attempt_id):
        self.calls += 1
        self.status_seen_at_submit = self.repository.get_item(item.id).status
        if self.after_submit is not None:
            self.after_submit(self.calls)
        if self.exception:
            raise self.exception
        evidence = ScanEvidence(
            observed_at_utc=NOW,
            latest_tracking=item.tracking if self.outcome is ScanOutcome.SUCCESS else None,
            container_no=item.container_no,
            sort_code=item.sort_code,
            logid="LOGID-SYNTHETIC" if self.outcome is ScanOutcome.ALREADY_IN_BAG else None,
        )
        return ScanResult(attempt_id, item.id, item.tracking, self.outcome, evidence)

    def recover_duplicate(self, result, session):
        from tt_auto_bag_scanner.domain.models import RecoveryResult

        return RecoveryResult(
            attempt_id=result.attempt_id,
            tracking=result.tracking,
            container_no=session.container_no,
            recovered=True,
            observed_at_utc=NOW,
            evidence_ref="synthetic-recovery",
        )


def make_repository(path, item_count=1, unchecked=()):
    repository = TaskRepository(path)
    items = [
        QueueItem(
            id=f"item-synthetic-{index}",
            task_id="task-synthetic",
            tracking=f"TRACK-SYNTHETIC-{index}",
            carrier_key="CARRIER-SYNTHETIC",
            sort_code="SORT-SYNTHETIC",
            container_no="BAG-SYNTHETIC",
            outbound_at_utc=NOW,
            source_row_numbers=(index + 1,),
        )
        for index in range(item_count)
    ]
    item = items[0]
    task = TaskSnapshot(
        id="task-synthetic",
        name="Synthetic task",
        state=TaskState.READY,
        item_ids=tuple(candidate.id for candidate in items),
        counts=TaskCounts.from_statuses([candidate.status for candidate in items]),
        created_at_utc=NOW,
        updated_at_utc=NOW,
        device_mode=DeviceMode.SIMULATED,
    )
    repository.create_task(task, items)
    checks = []
    for index, candidate in enumerate(items):
        if index in unchecked:
            continue
        check = PhysicalCheck(
            id=f"check-{candidate.id}",
            item_id=candidate.id,
            tracking=candidate.tracking,
            container_no=candidate.container_no,
            operator="operator-synthetic",
            checked_at_utc=NOW,
            method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
            evidence_ref="evidence-synthetic",
        )
        repository.record_physical_check(check)
        checks.append(check)
    repository.transition_task(
        item.task_id, TaskState.RUNNING, actor="operator", reason="test start"
    )
    session = VerifiedSession(
        serial="DEVICE-SYNTHETIC",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=item.container_no,
        sort_code=item.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=NOW,
    )
    return repository, item, checks[0] if checks else None, session


def test_worker_persists_before_single_controller_call_and_finishes_success(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository)
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert controller.status_seen_at_submit is ItemStatus.IN_FLIGHT
    assert repository.get_item(item.id).status is ItemStatus.SUCCESS
    assert repository.get_task(item.task_id).state is TaskState.COMPLETED


def test_worker_converts_ambiguous_exception_to_unknown_and_never_retries(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository, exception=TimeoutError("synthetic timeout"))
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert repository.get_item(item.id).status is ItemStatus.UNKNOWN
    assert repository.get_task(item.task_id).state is TaskState.BLOCKED


def test_result_persistence_failure_leaves_unknown_and_does_not_resend(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository)

    def fail_result_write(*args, **kwargs):
        raise OSError("synthetic database write failure")

    repository.finish_attempt = fail_result_write
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert repository.get_item(item.id).status is ItemStatus.UNKNOWN
    assert repository.latest_attempt(item.id)["status"] == "unknown"


def test_mismatched_success_evidence_becomes_unknown_and_blocks(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository)
    original_submit = controller.submit_once

    def mismatched_submit(current_item, current_session, current_check, attempt_id):
        original_submit(current_item, current_session, current_check, attempt_id)
        return ScanResult(
            attempt_id,
            current_item.id,
            current_item.tracking,
            ScanOutcome.SUCCESS,
            ScanEvidence(
                observed_at_utc=NOW,
                latest_tracking=current_item.tracking,
                container_no="BAG-DIFFERENT-SYNTHETIC",
                sort_code=current_item.sort_code,
            ),
        )

    controller.submit_once = mismatched_submit
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert repository.get_item(item.id).status is ItemStatus.UNKNOWN
    assert repository.get_task(item.task_id).state is TaskState.BLOCKED


def test_worker_recovery_keeps_duplicate_separate_from_new_success(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository, outcome=ScanOutcome.ALREADY_IN_BAG)
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    stats = repository.statistics(item.task_id)
    assert stats["success"] == 0
    assert stats["already_in_bag"] == 1
    assert stats["duplicate_recovered"] == 1
    assert controller.calls == 1


def test_worker_persists_duplicate_recovery_before_call_and_never_retries_failure(tmp_path) -> None:
    repository, item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    controller = InspectingController(repository, outcome=ScanOutcome.ALREADY_IN_BAG)

    def fail_recovery(result, current_session):
        assert repository.has_unresolved_recovery(item.task_id)
        raise TimeoutError("synthetic ambiguous recovery")

    controller.recover_duplicate = fail_recovery
    worker = ScanWorker(repository, item.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert repository.get_task(item.task_id).state is TaskState.BLOCKED
    assert repository.has_unresolved_recovery(item.task_id)
    events = [event["event_type"] for event in repository.events(task_id=item.task_id)]
    assert "duplicate_recovery_started" in events
    assert "duplicate_recovery_finished" in events


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [("pause", TaskState.PAUSED), ("stop", TaskState.STOPPED)],
)
def test_pause_and_stop_apply_at_safe_boundary_then_resume_remaining_items(
    tmp_path, action, expected_state
) -> None:
    repository, first_item, _, session = make_repository(tmp_path / "scanner.sqlite3", item_count=2)
    worker_reference = {}

    def request_at_first_result(call_count):
        if call_count == 1:
            getattr(worker_reference["worker"], f"request_{action}")()

    controller = InspectingController(repository, after_submit=request_at_first_result)
    worker = ScanWorker(repository, first_item.task_id, controller, lambda _: session)
    worker_reference["worker"] = worker

    worker.run()

    items = repository.list_items(first_item.task_id)
    assert repository.get_task(first_item.task_id).state is expected_state
    assert items[0].status is ItemStatus.SUCCESS
    assert items[1].status is ItemStatus.READY
    assert controller.calls == 1

    service = TaskService(repository)
    resumed = service.start_task(first_item.task_id, controller, lambda _: session)
    resumed.join(timeout=5)

    assert not resumed.is_alive()
    assert controller.calls == 2
    assert repository.get_task(first_item.task_id).state is TaskState.COMPLETED
    assert all(
        item.status is ItemStatus.SUCCESS for item in repository.list_items(first_item.task_id)
    )


def test_task_service_serializes_workers_across_tasks(tmp_path) -> None:
    repository, first_item, _, session = make_repository(tmp_path / "scanner.sqlite3")
    repository.transition_task(
        first_item.task_id,
        TaskState.PAUSED,
        actor="operator",
        reason="prepare worker service",
    )
    second_item = QueueItem(
        id="item-other-task",
        task_id="task-other",
        tracking="TRACK-OTHER-SYNTHETIC",
        carrier_key="CARRIER-SYNTHETIC",
        sort_code="SORT-SYNTHETIC",
        container_no="BAG-SYNTHETIC",
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
    )
    repository.create_task(
        TaskSnapshot(
            id="task-other",
            name="Other synthetic task",
            state=TaskState.READY,
            item_ids=(second_item.id,),
            counts=TaskCounts.from_statuses([second_item.status]),
            created_at_utc=NOW,
            updated_at_utc=NOW,
            device_mode=DeviceMode.SIMULATED,
        ),
        [second_item],
    )
    repository.record_physical_check(
        PhysicalCheck(
            id="check-other",
            item_id=second_item.id,
            tracking=second_item.tracking,
            container_no=second_item.container_no,
            operator="operator-synthetic",
            checked_at_utc=NOW,
            method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
            evidence_ref="evidence-synthetic",
        )
    )
    entered = threading.Event()
    release = threading.Event()
    controller = InspectingController(repository)
    base_submit = controller.submit_once

    def block_one_submission(current_item, current_session, current_check, attempt_id):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release the synthetic controller")
        return base_submit(current_item, current_session, current_check, attempt_id)

    controller.submit_once = block_one_submission
    service = TaskService(repository)
    worker = service.start_task(first_item.task_id, controller, lambda _: session)
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(RepositoryError, match="single device"):
            service.start_task("task-other", controller, lambda _: session)
        assert repository.get_task("task-other").state is TaskState.READY
    finally:
        release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert controller.calls == 1
    assert repository.get_task(first_item.task_id).state is TaskState.COMPLETED


def test_worker_skips_unverified_items_and_submits_verified_ones(tmp_path) -> None:
    repository, first, _, session = make_repository(
        tmp_path / "scanner.sqlite3", item_count=2, unchecked=(0,)
    )
    controller = InspectingController(repository)
    worker = ScanWorker(repository, first.task_id, controller, lambda _: session)

    worker.run()

    assert controller.calls == 1
    assert repository.get_item(first.id).status is ItemStatus.WAIT_PHYSICAL
    assert repository.get_item("item-synthetic-1").status is ItemStatus.SUCCESS
    assert repository.get_task(first.task_id).state is TaskState.PAUSED
