"""Create and revoke per-item physical evidence records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from tt_auto_bag_scanner.domain.models import (
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
)


class PhysicalCheckError(ValueError):
    """Physical evidence is missing or does not match the frozen target."""


class PhysicalCheckService:
    def record_manual(
        self,
        item: QueueItem,
        *,
        operator: str,
        evidence_ref: str,
        checked_at_utc: datetime,
    ) -> PhysicalCheck:
        self._validate_item(item)
        self._validate_actor_and_time(operator, checked_at_utc)
        if not evidence_ref.strip():
            raise PhysicalCheckError("evidence reference is required")
        return PhysicalCheck(
            id=str(uuid4()),
            item_id=item.id,
            tracking=item.tracking,
            container_no=item.container_no,
            operator=operator.strip(),
            checked_at_utc=checked_at_utc,
            method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
            evidence_ref=evidence_ref.strip(),
        )

    def record_barcode(
        self,
        item: QueueItem,
        *,
        operator: str,
        observed_tracking: str,
        observed_container_no: str,
        checked_at_utc: datetime,
    ) -> PhysicalCheck:
        self._validate_item(item)
        self._validate_actor_and_time(operator, checked_at_utc)
        if observed_tracking.strip() != item.tracking:
            raise PhysicalCheckError("observed tracking does not match the item")
        if observed_container_no.strip() != item.container_no:
            raise PhysicalCheckError("observed container does not match the item target")
        evidence_ref = (
            f"barcode-match:{item.id}:{observed_tracking.strip()}:{observed_container_no.strip()}"
        )
        return PhysicalCheck(
            id=str(uuid4()),
            item_id=item.id,
            tracking=item.tracking,
            container_no=item.container_no,
            operator=operator.strip(),
            checked_at_utc=checked_at_utc,
            method=PhysicalCheckMethod.BARCODE_MATCH,
            evidence_ref=evidence_ref,
        )

    @staticmethod
    def invalidate_if_changed(
        check: PhysicalCheck,
        *,
        previous_item: QueueItem,
        current_item: QueueItem,
        revoked_at_utc: datetime | None = None,
    ) -> PhysicalCheck:
        changed = (
            previous_item.id != current_item.id
            or previous_item.task_id != current_item.task_id
            or previous_item.tracking != current_item.tracking
            or previous_item.container_no != current_item.container_no
            or previous_item.sort_code != current_item.sort_code
        )
        if not changed:
            return check
        revoked_at = revoked_at_utc or datetime.now(UTC)
        if revoked_at.tzinfo is None or revoked_at.utcoffset() is None:
            raise PhysicalCheckError("revocation time must include a timezone")
        if check.revoked_at_utc is not None:
            return check
        return replace(check, revoked_at_utc=revoked_at)

    @staticmethod
    def _validate_item(item: QueueItem) -> None:
        if item.status not in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}:
            raise PhysicalCheckError("physical check requires an eligible, unsubmitted item")

    @staticmethod
    def _validate_actor_and_time(operator: str, checked_at_utc: datetime) -> None:
        if not operator.strip():
            raise PhysicalCheckError("operator is required")
        if checked_at_utc.tzinfo is None or checked_at_utc.utcoffset() is None:
            raise PhysicalCheckError("check time must include a timezone")
