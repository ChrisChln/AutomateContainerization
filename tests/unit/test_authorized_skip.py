from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.domain.models import ItemStatus, PhysicalCheckMethod, QueueItem
from tt_auto_bag_scanner.infrastructure.physical_check_service import (
    PhysicalCheckError,
    PhysicalCheckService,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _item() -> QueueItem:
    return QueueItem(
        id="item-skip-1",
        task_id="task-skip-1",
        tracking="TRACKING-SYN-SKIP",
        carrier_key="CARRIER-SYN",
        sort_code="SORT-SYN",
        container_no="CONTAINER-SYN",
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
        status=ItemStatus.WAIT_PHYSICAL,
    )


def test_authorized_skip_is_recorded_as_its_own_auditable_method() -> None:
    check = PhysicalCheckService().record_authorized_skip(
        _item(), operator=" operator-syn ", checked_at_utc=NOW
    )

    assert check.method is PhysicalCheckMethod.AUTHORIZED_SKIP
    assert check.operator == "operator-syn"
    assert check.evidence_ref.startswith("authorized-skip:")
    assert check.is_valid_for(_item())


def test_authorized_skip_requires_an_operator() -> None:
    with pytest.raises(PhysicalCheckError):
        PhysicalCheckService().record_authorized_skip(_item(), operator=" ", checked_at_utc=NOW)
