"""Explicit state transition rules for tasks and shipment rows."""

from __future__ import annotations

from tt_auto_bag_scanner.domain.models import ItemStatus, TaskState


class StateTransitionError(ValueError):
    """Raised when a state change violates scanner safety rules."""


_TASK_TRANSITIONS = {
    TaskState.DRAFT: {TaskState.READY, TaskState.STOPPED},
    TaskState.READY: {TaskState.RUNNING, TaskState.STOPPED},
    TaskState.RUNNING: {
        TaskState.PAUSING,
        TaskState.PAUSED,
        TaskState.BLOCKED,
        TaskState.STOPPED,
        TaskState.COMPLETED,
    },
    TaskState.PAUSING: {TaskState.PAUSED, TaskState.BLOCKED, TaskState.STOPPED},
    TaskState.PAUSED: {TaskState.RUNNING, TaskState.STOPPED, TaskState.BLOCKED},
    TaskState.BLOCKED: {TaskState.PAUSED, TaskState.STOPPED},
    TaskState.STOPPED: {TaskState.READY, TaskState.RUNNING},
    TaskState.COMPLETED: set(),
}

_ITEM_TRANSITIONS = {
    ItemStatus.WAIT_PHYSICAL: {ItemStatus.READY, ItemStatus.CANCELLED},
    ItemStatus.READY: {ItemStatus.IN_FLIGHT, ItemStatus.WAIT_PHYSICAL, ItemStatus.CANCELLED},
    ItemStatus.IN_FLIGHT: {
        ItemStatus.SUCCESS,
        ItemStatus.ALREADY_IN_BAG,
        ItemStatus.UNKNOWN,
        ItemStatus.REJECTED,
    },
    ItemStatus.UNKNOWN: {ItemStatus.READY, ItemStatus.REJECTED, ItemStatus.CANCELLED},
    ItemStatus.SUCCESS: set(),
    ItemStatus.ALREADY_IN_BAG: set(),
    ItemStatus.REJECTED: set(),
    ItemStatus.CANCELLED: set(),
}


def transition_task(
    current: TaskState,
    target: TaskState,
    *,
    unresolved_items: int = 0,
) -> TaskState:
    if target not in _TASK_TRANSITIONS[current]:
        raise StateTransitionError(f"illegal task transition: {current} -> {target}")
    if target is TaskState.COMPLETED and unresolved_items:
        raise StateTransitionError("unresolved items prevent task completion")
    if current is TaskState.STOPPED and target is TaskState.RUNNING and unresolved_items:
        raise StateTransitionError("unresolved items prevent task resume")
    return target


def transition_item(
    current: ItemStatus,
    target: ItemStatus,
    *,
    physical_check_valid: bool = False,
    reconciliation_confirmed: bool = False,
) -> ItemStatus:
    if target not in _ITEM_TRANSITIONS[current]:
        raise StateTransitionError(f"illegal item transition: {current} -> {target}")
    if target is ItemStatus.IN_FLIGHT and not physical_check_valid:
        raise StateTransitionError("valid physical check required before submission")
    if (
        current is ItemStatus.WAIT_PHYSICAL
        and target is ItemStatus.READY
        and not physical_check_valid
    ):
        raise StateTransitionError("valid physical check required before readiness")
    if current is ItemStatus.UNKNOWN and target is ItemStatus.READY:
        if not reconciliation_confirmed:
            raise StateTransitionError("human reconciliation is required before retry")
        if not physical_check_valid:
            raise StateTransitionError("new physical check is required after reconciliation")
    return target
