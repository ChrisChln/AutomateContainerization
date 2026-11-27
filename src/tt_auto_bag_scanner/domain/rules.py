"""Small, side-effect-free eligibility and confirmation rules."""

from __future__ import annotations

from tt_auto_bag_scanner.domain.models import (
    DeviceMode,
    ItemStatus,
    PhysicalCheck,
    QueueItem,
    TaskSnapshot,
    VerifiedSession,
)


def can_start_task(task: TaskSnapshot, *, unresolved_items: int = 0) -> bool:
    return (
        task.state.value in {"ready", "paused"}
        and unresolved_items == 0
        and (task.device_mode is DeviceMode.SIMULATED or task.real_mode_authorized)
    )


def can_submit_item(
    item: QueueItem,
    physical_check: PhysicalCheck | None,
    session: VerifiedSession | None,
) -> bool:
    return (
        item.status is ItemStatus.READY
        and physical_check is not None
        and physical_check.is_valid_for(item)
        and session is not None
        and session.valid
        and session.container_no == item.container_no
        and session.sort_code == item.sort_code
    )
