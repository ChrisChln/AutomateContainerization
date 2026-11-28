"""Task lifecycle orchestration and safe manual reconciliation."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from tt_auto_bag_scanner.application.scan_worker import ScanWorker
from tt_auto_bag_scanner.domain.device import WmsController
from tt_auto_bag_scanner.domain.models import (
    ItemStatus,
    PhysicalCheck,
    QueueItem,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.domain.rules import can_start_task
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository


class TaskService:
    def __init__(self, repository: TaskRepository, *, actor: str = "local-operator") -> None:
        self.repository = repository
        self.actor = actor
        self._lock = threading.RLock()
        self._workers: dict[str, ScanWorker] = {}

    def start_task(
        self,
        task_id: str,
        controller: WmsController,
        session_provider: Callable[[QueueItem], VerifiedSession],
        *,
        real_mode_authorized: bool = False,
        scan_interval: tuple[int, int, str] = (5, 30, "none"),
        priority_item_ids: Iterable[str] = (),
    ) -> ScanWorker:
        with self._lock:
            for running_task_id, worker in self._workers.items():
                if worker.is_alive():
                    raise RepositoryError(
                        f"worker for task {running_task_id} already owns the single device"
                    )
            current = self._workers.get(task_id)
            if current is not None and current.is_alive():
                raise RepositoryError("a worker already owns this task and device")
            task = self.repository.get_task(task_id)
            if getattr(controller, "device_mode", None) is not task.device_mode:
                raise RepositoryError(
                    "WMS controller device mode does not match the persisted task mode"
                )
            if task.state not in {TaskState.READY, TaskState.PAUSED, TaskState.STOPPED}:
                raise RepositoryError(f"task cannot start from {task.state.value}")
            if self.repository.has_blocking_unresolved(task_id):
                raise RepositoryError(
                    "in-flight submission or unfinished duplicate recovery requires reconciliation"
                )
            if task.device_mode.value == "real":
                if not real_mode_authorized or not task.real_mode_authorized:
                    raise RepositoryError("explicit site authorization is required for real mode")
            if task.state is not TaskState.STOPPED and not can_start_task(task, unresolved_items=0):
                raise RepositoryError("task start conditions are not satisfied")
            self.repository.transition_task(
                task_id,
                TaskState.RUNNING,
                actor=self.actor,
                reason=(
                    "operator explicitly authorized real-mode start"
                    if task.device_mode.value == "real"
                    else "operator started task"
                ),
            )
            worker = ScanWorker(
                self.repository,
                task_id,
                controller,
                session_provider,
                actor=self.actor,
                scan_interval=scan_interval,
                priority_item_ids=priority_item_ids,
            )
            self._workers[task_id] = worker
            worker.start()
            return worker

    def pause_task(self, task_id: str) -> None:
        with self._lock:
            task = self.repository.get_task(task_id)
            if task.state is TaskState.RUNNING:
                self.repository.transition_task(
                    task_id,
                    TaskState.PAUSING,
                    actor=self.actor,
                    reason="operator requested pause at safe boundary",
                )
            worker = self._workers.get(task_id)
            if worker is not None and worker.is_alive():
                worker.request_pause()
                return
            if self.repository.get_task(task_id).state is TaskState.PAUSING:
                self.repository.transition_task(
                    task_id,
                    TaskState.PAUSED,
                    actor=self.actor,
                    reason="task paused without an in-flight operation",
                )

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
            task = self.repository.get_task(task_id)
            if worker is not None and worker.is_alive():
                if task.state is TaskState.RUNNING:
                    self.repository.transition_task(
                        task_id,
                        TaskState.PAUSING,
                        actor=self.actor,
                        reason="operator requested stop at safe boundary",
                    )
                worker.request_stop()
                return
            if task.state in {
                TaskState.DRAFT,
                TaskState.READY,
                TaskState.PAUSED,
                TaskState.BLOCKED,
            }:
                self.repository.transition_task(
                    task_id,
                    TaskState.STOPPED,
                    actor=self.actor,
                    reason="operator stopped task",
                )
                return
            raise RepositoryError("running tasks stop only at a safe boundary")

    def reconcile_unknown(
        self,
        item_id: str,
        target: ItemStatus,
        *,
        actor: str,
        reason: str,
        physical_check: PhysicalCheck | None = None,
    ) -> None:
        self.repository.reconcile_item(
            item_id,
            target,
            actor=actor,
            reason=reason,
            physical_check=physical_check,
        )

    def cancel_unsubmitted_item(self, item_id: str) -> None:
        self.repository.cancel_unsubmitted_item(
            item_id,
            actor=self.actor,
            reason="operator cancelled before submission",
        )

    def reconcile_duplicate_recovery(
        self,
        attempt_id: str,
        *,
        tracking: str,
        container_no: str,
        logid: str,
        actor: str,
        reason: str,
        evidence_ref: str,
    ) -> None:
        self.repository.reconcile_duplicate_recovery(
            attempt_id,
            tracking=tracking,
            container_no=container_no,
            logid=logid,
            actor=actor,
            reason=reason,
            evidence_ref=evidence_ref,
        )

    def active_worker(self, task_id: str) -> ScanWorker | None:
        worker = self._workers.get(task_id)
        return worker if worker is not None and worker.is_alive() else None
