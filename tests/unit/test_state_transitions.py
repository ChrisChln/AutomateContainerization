import pytest

from tt_auto_bag_scanner.domain.models import ItemStatus, TaskState
from tt_auto_bag_scanner.domain.states import StateTransitionError, transition_item, transition_task


def test_running_task_pauses_at_safe_boundary_and_resumes() -> None:
    assert transition_task(TaskState.RUNNING, TaskState.PAUSING) is TaskState.PAUSING
    assert transition_task(TaskState.PAUSING, TaskState.PAUSED) is TaskState.PAUSED
    assert transition_task(TaskState.PAUSED, TaskState.RUNNING) is TaskState.RUNNING


def test_stopped_task_can_resume_only_without_unresolved_attempts() -> None:
    assert transition_task(TaskState.RUNNING, TaskState.STOPPED) is TaskState.STOPPED
    assert transition_task(TaskState.STOPPED, TaskState.RUNNING) is TaskState.RUNNING
    with pytest.raises(StateTransitionError, match="unresolved"):
        transition_task(TaskState.STOPPED, TaskState.RUNNING, unresolved_items=1)


def test_task_cannot_complete_with_unknown_items() -> None:
    with pytest.raises(StateTransitionError, match="unresolved"):
        transition_task(
            TaskState.RUNNING,
            TaskState.COMPLETED,
            unresolved_items=1,
        )


def test_unverified_item_cannot_enter_flight() -> None:
    with pytest.raises(StateTransitionError, match="physical"):
        transition_item(
            ItemStatus.WAIT_PHYSICAL,
            ItemStatus.READY,
            physical_check_valid=False,
        )


def test_ambiguous_item_cannot_be_retried_without_human_reconciliation() -> None:
    with pytest.raises(StateTransitionError, match="reconciliation"):
        transition_item(ItemStatus.UNKNOWN, ItemStatus.READY)

    assert (
        transition_item(
            ItemStatus.UNKNOWN,
            ItemStatus.READY,
            reconciliation_confirmed=True,
            physical_check_valid=True,
        )
        is ItemStatus.READY
    )


def test_duplicate_is_a_terminal_confirmed_outcome_separate_from_success() -> None:
    assert (
        transition_item(ItemStatus.IN_FLIGHT, ItemStatus.ALREADY_IN_BAG)
        is ItemStatus.ALREADY_IN_BAG
    )
    assert transition_item(ItemStatus.IN_FLIGHT, ItemStatus.SUCCESS) is ItemStatus.SUCCESS
