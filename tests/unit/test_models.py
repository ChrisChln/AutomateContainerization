from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    ScanEvidence,
    ScanOutcome,
    ScanResult,
    TaskCounts,
)


def make_item() -> QueueItem:
    return QueueItem(
        id="item-test",
        task_id="task-test",
        tracking="000123456789012345",
        carrier_key="SYNTHETIC_CARRIER",
        sort_code="SYNTHETIC_SORT",
        container_no="SYNTHETIC_CONTAINER",
        outbound_at_utc=datetime(2026, 11, 26, 12, 0, tzinfo=UTC),
        source_row_numbers=(2,),
    )


def test_queue_item_preserves_tracking_number_as_text() -> None:
    item = make_item()

    assert item.tracking == "000123456789012345"
    assert item.status is ItemStatus.WAIT_PHYSICAL


def test_carrier_config_requires_sort_and_container() -> None:
    with pytest.raises(ValueError, match="container"):
        CarrierConfig(
            carrier_key="SYNTHETIC_CARRIER",
            display_name="Synthetic Carrier",
            aliases=(),
            sort_code="SYNTHETIC_SORT",
            container_no=" ",
            enabled=True,
            version=1,
            updated_at_utc=datetime(2026, 11, 26, 12, 0, tzinfo=UTC),
        )


def test_physical_check_matches_item_and_target_container() -> None:
    item = make_item()
    check = PhysicalCheck(
        id="check-test",
        item_id=item.id,
        tracking=item.tracking,
        container_no=item.container_no,
        operator="local-operator",
        checked_at_utc=datetime(2026, 11, 26, 12, 1, tzinfo=UTC),
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="manual-confirmation:test-record",
    )

    assert check.is_valid_for(item)


def test_revoked_or_mismatched_physical_check_is_invalid() -> None:
    item = make_item()
    check = PhysicalCheck(
        id="check-test",
        item_id=item.id,
        tracking=item.tracking,
        container_no="OTHER_CONTAINER",
        operator="local-operator",
        checked_at_utc=datetime(2026, 11, 26, 12, 1, tzinfo=UTC),
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="manual-confirmation:test-record",
        revoked_at_utc=datetime(2026, 11, 26, 12, 2, tzinfo=UTC),
    )

    assert not check.is_valid_for(item)


def test_scan_success_requires_matching_latest_record_and_container_evidence() -> None:
    item = make_item()
    evidence = ScanEvidence(
        observed_at_utc=datetime(2026, 11, 26, 12, 3, tzinfo=UTC),
        latest_tracking=item.tracking,
        container_no=item.container_no,
        sort_code=item.sort_code,
    )

    result = ScanResult(
        attempt_id="attempt-test",
        item_id=item.id,
        tracking=item.tracking,
        outcome=ScanOutcome.SUCCESS,
        evidence=evidence,
    )

    assert result.outcome is ScanOutcome.SUCCESS

    bad_evidence = ScanEvidence(
        observed_at_utc=datetime(2026, 11, 26, 12, 3, tzinfo=UTC),
        latest_tracking="A_DIFFERENT_TRACKING",
        container_no=item.container_no,
        sort_code=item.sort_code,
    )
    with pytest.raises(ValueError, match="latest tracking"):
        ScanResult(
            attempt_id="attempt-test",
            item_id=item.id,
            tracking=item.tracking,
            outcome=ScanOutcome.SUCCESS,
            evidence=bad_evidence,
        )


def test_duplicate_result_requires_logid_and_does_not_count_as_success() -> None:
    item = make_item()
    evidence = ScanEvidence(
        observed_at_utc=datetime(2026, 11, 26, 12, 3, tzinfo=UTC),
        container_no=item.container_no,
        sort_code=item.sort_code,
        error_text="已扫描包裹已在当前大包中",
        logid="LOGID-SYNTHETIC",
    )

    result = ScanResult(
        attempt_id="attempt-test",
        item_id=item.id,
        tracking=item.tracking,
        outcome=ScanOutcome.ALREADY_IN_BAG,
        evidence=evidence,
    )

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG


def test_task_counts_cover_each_mutually_exclusive_state() -> None:
    counts = TaskCounts.from_statuses(
        [ItemStatus.SUCCESS, ItemStatus.ALREADY_IN_BAG, ItemStatus.UNKNOWN]
    )

    assert counts.total == 3
    assert counts.confirmed_complete == 2
    assert counts.unresolved == 1
    assert sum(counts.by_status.values()) == counts.total
