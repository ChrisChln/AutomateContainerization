from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.domain.models import (
    ItemStatus,
    PhysicalCheckMethod,
    QueueItem,
)
from tt_auto_bag_scanner.infrastructure.physical_check_service import (
    PhysicalCheckError,
    PhysicalCheckService,
)


def make_item(container="BAG-A", task="task-synthetic"):
    return QueueItem(
        id="item-synthetic",
        task_id=task,
        tracking="SYN-TRACKING",
        carrier_key="SYN-CARRIER",
        sort_code="SYN-SORT",
        container_no=container,
        outbound_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
        source_row_numbers=(2,),
        status=ItemStatus.WAIT_PHYSICAL,
    )


def test_manual_check_requires_operator_and_matching_item_container_and_evidence():
    service = PhysicalCheckService()
    checked_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    item = make_item()
    check = service.record_manual(
        item,
        operator="operator-synthetic",
        evidence_ref="local-confirmation:synthetic",
        checked_at_utc=checked_at,
    )

    assert check.is_valid_for(item)
    assert check.container_no == item.container_no
    with pytest.raises(PhysicalCheckError, match="operator"):
        service.record_manual(
            item, operator=" ", evidence_ref="evidence", checked_at_utc=checked_at
        )
    with pytest.raises(PhysicalCheckError, match="container"):
        service.record_barcode(
            item,
            operator="operator-synthetic",
            observed_tracking=item.tracking,
            observed_container_no="OTHER-BAG",
            checked_at_utc=checked_at,
        )


def test_container_or_task_snapshot_change_revokes_previous_check():
    service = PhysicalCheckService()
    check = service.record_manual(
        make_item(),
        operator="operator-synthetic",
        evidence_ref="local-confirmation:synthetic",
        checked_at_utc=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    changed_item = make_item(container="BAG-B")

    revoked = service.invalidate_if_changed(
        check,
        previous_item=make_item(),
        current_item=changed_item,
        revoked_at_utc=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
    )

    assert revoked.revoked_at_utc is not None
    assert not revoked.is_valid_for(changed_item)


def test_barcode_check_requires_both_exact_tracking_and_container():
    service = PhysicalCheckService()
    item = make_item()

    with pytest.raises(PhysicalCheckError, match="tracking"):
        service.record_barcode(
            item,
            operator="operator-synthetic",
            observed_tracking="SYN-OTHER",
            observed_container_no="BAG-A",
            checked_at_utc=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        )
    check = service.record_barcode(
        item,
        operator="operator-synthetic",
        observed_tracking=item.tracking,
        observed_container_no=item.container_no,
        checked_at_utc=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    assert check.method is PhysicalCheckMethod.BARCODE_MATCH
