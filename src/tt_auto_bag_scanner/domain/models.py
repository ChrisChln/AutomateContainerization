"""Immutable records shared across the scanner application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class PageState(StrEnum):
    LOGIN = "login"
    HOME = "home"
    CONTAINER_MENU = "container_menu"
    SINGLE_EMPTY = "single_empty"
    BAG_READY = "bag_ready"
    SCAN_RESULT = "scan_result"
    DUPLICATE_DIALOG = "duplicate_dialog"
    UNKNOWN = "unknown"


class TaskState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    STOPPED = "stopped"


class ItemStatus(StrEnum):
    WAIT_PHYSICAL = "wait_physical"
    READY = "ready"
    IN_FLIGHT = "in_flight"
    SUCCESS = "success"
    ALREADY_IN_BAG = "already_in_bag"
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ScanOutcome(StrEnum):
    SUCCESS = "success"
    ALREADY_IN_BAG = "already_in_bag"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class PhysicalCheckMethod(StrEnum):
    MANUAL_CONFIRMATION = "manual_confirmation"
    BARCODE_MATCH = "barcode_match"
    # Site-approved exception: the operator explicitly skipped the physical check.
    AUTHORIZED_SKIP = "authorized_skip"


class DeviceMode(StrEnum):
    SIMULATED = "simulated"
    REAL = "real"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    serial: str
    model: str = ""
    manufacturer: str = ""
    android_version: str = ""
    sdk_version: str = ""
    state: str = "unknown"
    authorized: bool = False
    app_version: str = ""


@dataclass(frozen=True, slots=True)
class XmlSnapshot:
    xml: str
    captured_at_utc: datetime
    package_name: str = ""
    activity_name: str = ""
    redacted_ref: str = ""


@dataclass(frozen=True, slots=True)
class InputField:
    resource_id: str
    text: str
    class_name: str
    bounds: tuple[int, int, int, int]
    focused: bool
    editable: bool


@dataclass(frozen=True, slots=True)
class PageObservation:
    state: PageState
    visible_labels: Mapping[str, str] = field(default_factory=dict)
    focused_inputs: tuple[InputField, ...] = ()
    latest_tracking_records: tuple[str, ...] = ()
    dialog_text: str = ""
    logid: str | None = None
    redacted_xml_ref: str = ""
    # Every visible enabled input on the page; focus is reported per field.
    input_fields: tuple[InputField, ...] = ()
    # Inputs located inside the recognized WMS dialog subtree only.
    dialog_inputs: tuple[InputField, ...] = ()
    # Tracking number printed inside the exact duplicate dialog body.
    dialog_tracking: str = ""
    duplicate_in_bag: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_labels", MappingProxyType(dict(self.visible_labels)))
        object.__setattr__(self, "focused_inputs", tuple(self.focused_inputs))
        object.__setattr__(self, "latest_tracking_records", tuple(self.latest_tracking_records))
        object.__setattr__(self, "input_fields", tuple(self.input_fields))
        object.__setattr__(self, "dialog_inputs", tuple(self.dialog_inputs))


@dataclass(frozen=True, slots=True)
class CarrierConfig:
    carrier_key: str
    display_name: str
    aliases: tuple[str, ...]
    sort_code: str
    container_no: str
    enabled: bool
    version: int
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        if not self.carrier_key.strip():
            raise ValueError("carrier key is required")
        if not self.sort_code.strip():
            raise ValueError("sort code is required")
        if not self.container_no.strip():
            raise ValueError("container is required")
        if self.version < 1:
            raise ValueError("version must be positive")
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True, slots=True)
class PhysicalCheck:
    id: str
    item_id: str
    tracking: str
    container_no: str
    operator: str
    checked_at_utc: datetime
    method: PhysicalCheckMethod
    evidence_ref: str
    revoked_at_utc: datetime | None = None

    def is_valid_for(self, item: QueueItem) -> bool:
        return (
            self.revoked_at_utc is None
            and self.item_id == item.id
            and self.tracking == item.tracking
            and self.container_no == item.container_no
            and bool(self.operator.strip())
            and bool(self.evidence_ref.strip())
        )


@dataclass(frozen=True, slots=True)
class QueueItem:
    id: str
    task_id: str
    tracking: str
    carrier_key: str
    sort_code: str
    container_no: str
    outbound_at_utc: datetime
    source_row_numbers: tuple[int, ...]
    status: ItemStatus = ItemStatus.WAIT_PHYSICAL
    physical_check_id: str | None = None
    source_hash: str = ""
    created_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tracking.strip():
            raise ValueError("tracking is required")
        if not self.container_no.strip():
            raise ValueError("container is required")
        object.__setattr__(self, "source_row_numbers", tuple(self.source_row_numbers))


@dataclass(frozen=True, slots=True)
class VerifiedSession:
    serial: str
    warehouse: str
    container_no: str
    sort_code: str
    session_id: str
    verified_at_utc: datetime
    valid: bool = True


@dataclass(frozen=True, slots=True)
class ScanEvidence:
    observed_at_utc: datetime
    latest_tracking: str | None = None
    container_no: str | None = None
    sort_code: str | None = None
    error_text: str = ""
    logid: str | None = None
    redacted_xml_ref: str = ""


@dataclass(frozen=True, slots=True)
class ScanResult:
    attempt_id: str
    item_id: str
    tracking: str
    outcome: ScanOutcome
    evidence: ScanEvidence
    recovery_status: str = "not_required"

    def __post_init__(self) -> None:
        if self.outcome is ScanOutcome.SUCCESS:
            if self.evidence.latest_tracking != self.tracking:
                raise ValueError("success requires matching latest tracking evidence")
            if not self.evidence.container_no:
                raise ValueError("success requires container evidence")
            if not self.evidence.sort_code:
                raise ValueError("success requires sort evidence")
        if self.outcome is ScanOutcome.ALREADY_IN_BAG and not self.evidence.logid:
            raise ValueError("duplicate result requires Logid evidence")


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    attempt_id: str
    tracking: str
    container_no: str
    recovered: bool
    observed_at_utc: datetime
    logid: str | None = None
    evidence_ref: str = ""


@dataclass(frozen=True, slots=True)
class ImportMapping:
    tracking_column: str
    carrier_column: str
    status_column: str
    outbound_at_column: str
    report_timezone: str
    threshold_minutes: int = 30
    sort_code_column: str | None = None
    sheet_name: str | None = None
    header_row: int = 1
    sort_code_overrides: tuple[tuple[int, str], ...] | Mapping[int, str] = ()

    def __post_init__(self) -> None:
        values = (
            self.sort_code_overrides.items()
            if isinstance(self.sort_code_overrides, Mapping)
            else self.sort_code_overrides
        )
        normalized: dict[int, str] = {}
        try:
            for source_row_number, sort_code in values:
                if isinstance(source_row_number, bool):
                    raise ValueError
                row_number = int(source_row_number)
                code = str(sort_code).strip() if sort_code is not None else ""
                if row_number < 1 or not code:
                    raise ValueError
                if row_number in normalized and normalized[row_number] != code:
                    raise ValueError
                normalized[row_number] = code
        except (TypeError, ValueError) as error:
            raise ValueError(
                "sort-code overrides require positive source rows and non-empty codes"
            ) from error
        object.__setattr__(self, "sort_code_overrides", tuple(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class ImportSummary:
    source_hash: str
    source_name: str
    total_rows: int
    eligible_rows: int
    excluded_rows: int
    conflict_count: int
    mapping: ImportMapping
    created_at_utc: datetime


@dataclass(frozen=True, slots=True)
class TaskCounts:
    total: int
    by_status: Mapping[ItemStatus, int]

    def __post_init__(self) -> None:
        normalized = {status: int(self.by_status.get(status, 0)) for status in ItemStatus}
        if sum(normalized.values()) != self.total:
            raise ValueError("status counts must equal total")
        object.__setattr__(self, "by_status", MappingProxyType(normalized))

    @property
    def confirmed_complete(self) -> int:
        return self.by_status[ItemStatus.SUCCESS] + self.by_status[ItemStatus.ALREADY_IN_BAG]

    @property
    def unresolved(self) -> int:
        return self.by_status[ItemStatus.UNKNOWN] + self.by_status[ItemStatus.IN_FLIGHT]

    @classmethod
    def from_statuses(cls, statuses: list[ItemStatus] | tuple[ItemStatus, ...]) -> TaskCounts:
        counts = {status: 0 for status in ItemStatus}
        for status in statuses:
            counts[status] += 1
        return cls(total=len(statuses), by_status=counts)


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    name: str
    state: TaskState
    item_ids: tuple[str, ...]
    counts: TaskCounts
    created_at_utc: datetime
    updated_at_utc: datetime
    device_mode: DeviceMode = DeviceMode.SIMULATED
    real_mode_authorized: bool = False
    source_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_ids", tuple(self.item_ids))


@dataclass(frozen=True, slots=True)
class ImportCandidate:
    tracking: str
    carrier_value: str
    status_value: str
    outbound_at_utc: datetime | None
    source_row_number: int
    carrier_key: str | None = None
    sort_code: str | None = None
    container_no: str | None = None
