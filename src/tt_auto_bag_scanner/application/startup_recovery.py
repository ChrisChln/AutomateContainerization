"""Fail-closed startup recovery for persisted in-flight writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tt_auto_bag_scanner.infrastructure.repository import TaskRepository


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    unknown_attempt_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]


def recover_on_startup(
    repository: TaskRepository,
    *,
    at_utc: datetime | None = None,
) -> StartupRecoveryResult:
    unknown_attempt_ids = repository.recover_inflight(at_utc=at_utc)
    blocked = tuple(task.id for task in repository.list_tasks() if task.state.value == "blocked")
    return StartupRecoveryResult(unknown_attempt_ids, blocked)
