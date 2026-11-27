"""Single-threaded scan worker with a persisted attempt before each WMS write."""

from __future__ import annotations

import threading
from collections.abc import Callable

from tt_auto_bag_scanner.domain.device import WmsController
from tt_auto_bag_scanner.domain.models import (
    ItemStatus,
    QueueItem,
    RecoveryResult,
    ScanOutcome,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.domain.rules import can_submit_item
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository


class ScanWorker(threading.Thread):
    def __init__(
        self,
        repository: TaskRepository,
        task_id: str,
        controller: WmsController,
        session_provider: Callable[[QueueItem], VerifiedSession],
        *,
        actor: str = "scanner",
    ) -> None:
        super().__init__(name=f"scan-worker-{task_id}", daemon=False)
        self.repository = repository
        self.task_id = task_id
        self.controller = controller
        self.session_provider = session_provider
        self.actor = actor
        self._pause_requested = threading.Event()
        self._stop_requested = threading.Event()
        self.completed = threading.Event()
        self.error: str | None = None

    def request_pause(self) -> None:
        self._pause_requested.set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        try:
            self._run_serially()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._block_task("worker error", self.error)
        finally:
            self.repository.close()
            self.completed.set()

    def _run_serially(self) -> None:
        task = self.repository.get_task(self.task_id)
        if task.state is not TaskState.RUNNING:
            raise RepositoryError("worker requires a persisted RUNNING task")
        if self.repository.has_unresolved(self.task_id):
            self._block_task(
                "unresolved state", "task has UNKNOWN, IN_FLIGHT, or unfinished duplicate recovery"
            )
            return
        for item in self.repository.list_items(self.task_id):
            if self._stop_requested.is_set():
                self._stop_at_boundary()
                return
            if self._pause_requested.is_set():
                self._pause_at_boundary()
                return
            if item.status in {
                ItemStatus.SUCCESS,
                ItemStatus.ALREADY_IN_BAG,
                ItemStatus.REJECTED,
                ItemStatus.CANCELLED,
            }:
                continue
            if item.status is ItemStatus.WAIT_PHYSICAL:
                continue
            if item.status is not ItemStatus.READY:
                self._block_task("unsafe item state", f"item {item.id} is {item.status.value}")
                return
            try:
                current_item = self.repository.get_item(item.id)
                physical_check = self.repository.get_physical_check(item.id)
                session = self.session_provider(current_item)
            except Exception as exc:
                self._block_task("preflight failed", str(exc))
                return
            if not can_submit_item(current_item, physical_check, session):
                self._block_task(
                    "preflight failed", "physical evidence or verified session does not match item"
                )
                return
            try:
                self.repository.record_session_verified(
                    current_item.id,
                    session,
                    actor=self.actor,
                )
                attempt_id = self.repository.begin_attempt(
                    current_item.id,
                    session,
                    physical_check,
                    actor=self.actor,
                )
            except Exception as exc:
                self._block_task("attempt could not be persisted", str(exc))
                return

            try:
                result = self.controller.submit_once(
                    current_item,
                    session,
                    physical_check,
                    attempt_id,
                )
                if result.attempt_id != attempt_id:
                    raise RepositoryError("controller result does not match the persisted attempt")
                self.repository.finish_attempt(attempt_id, result, actor=self.actor)
            except Exception as exc:
                try:
                    self.repository.mark_attempt_unknown(
                        attempt_id,
                        f"submission outcome is ambiguous: {type(exc).__name__}: {exc}",
                        actor=self.actor,
                    )
                finally:
                    self.error = f"ambiguous scan result: {type(exc).__name__}: {exc}"
                return

            if result.outcome is ScanOutcome.ALREADY_IN_BAG:
                if not self._recover_duplicate(result, session):
                    return
            elif result.outcome is not ScanOutcome.SUCCESS:
                self._block_task("scan result needs review", result.outcome.value)
                return

            if self._stop_requested.is_set():
                self._stop_at_boundary()
                return
            if self._pause_requested.is_set():
                self._pause_at_boundary()
                return

        task_items = self.repository.list_items(self.task_id)
        unresolved = [
            item
            for item in task_items
            if item.status
            not in {
                ItemStatus.SUCCESS,
                ItemStatus.ALREADY_IN_BAG,
                ItemStatus.REJECTED,
                ItemStatus.CANCELLED,
            }
        ]
        if unresolved:
            self._pause_at_boundary("task contains items that were not submitted")
        else:
            self._transition_if_current(
                TaskState.COMPLETED, "all items reached a confirmed terminal state"
            )

    def _recover_duplicate(self, result, session: VerifiedSession) -> bool:
        try:
            self.repository.begin_recovery(result.attempt_id, actor=self.actor)
            recovery = self.controller.recover_duplicate(result, session)
            if not isinstance(recovery, RecoveryResult):
                raise RepositoryError("duplicate recovery returned an invalid result")
            if recovery.attempt_id != result.attempt_id:
                raise RepositoryError("duplicate recovery does not match the scan attempt")
            self.repository.record_recovery(recovery, actor=self.actor)
        except Exception as exc:
            recovery = RecoveryResult(
                attempt_id=result.attempt_id,
                tracking=result.tracking,
                container_no=session.container_no,
                recovered=False,
                observed_at_utc=result.evidence.observed_at_utc,
                logid=result.evidence.logid,
                evidence_ref=f"recovery failed: {type(exc).__name__}",
            )
            try:
                self.repository.record_recovery(recovery, actor=self.actor)
            except Exception as persistence_error:
                self.error = f"recovery and audit persistence failed: {persistence_error}"
            self._block_task("duplicate recovery failed", str(exc))
            return False
        if not recovery.recovered:
            self._block_task(
                "duplicate recovery needs review", recovery.evidence_ref or "recovery unconfirmed"
            )
            return False
        return True

    def _pause_at_boundary(self, reason: str = "operator requested pause at safe boundary") -> None:
        task = self.repository.get_task(self.task_id)
        if task.state is TaskState.RUNNING:
            self.repository.transition_task(
                self.task_id,
                TaskState.PAUSING,
                actor=self.actor,
                reason=reason,
            )
        task = self.repository.get_task(self.task_id)
        if task.state is TaskState.PAUSING:
            self.repository.transition_task(
                self.task_id,
                TaskState.PAUSED,
                actor=self.actor,
                reason="worker reached a safe boundary",
            )

    def _stop_at_boundary(self) -> None:
        task = self.repository.get_task(self.task_id)
        if task.state is TaskState.RUNNING:
            self.repository.transition_task(
                self.task_id,
                TaskState.PAUSING,
                actor=self.actor,
                reason="operator requested stop at safe boundary",
            )
        self._transition_if_current(TaskState.STOPPED, "worker stopped at safe boundary")

    def _block_task(self, event: str, detail: str) -> None:
        self.error = detail
        try:
            task = self.repository.get_task(self.task_id)
            if task.state in {TaskState.RUNNING, TaskState.PAUSING, TaskState.PAUSED}:
                self.repository.transition_task(
                    self.task_id,
                    TaskState.BLOCKED,
                    actor=self.actor,
                    reason=f"{event}: {detail}",
                )
        except RepositoryError:
            # The persisted UNKNOWN/in-flight state itself prevents any future submission.
            return

    def _transition_if_current(self, target: TaskState, reason: str) -> None:
        current = self.repository.get_task(self.task_id).state
        if current is target:
            return
        self.repository.transition_task(
            self.task_id,
            target,
            actor=self.actor,
            reason=reason,
        )
