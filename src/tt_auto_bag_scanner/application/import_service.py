"""Build immutable, reviewable shipment queues from read-only XLSX imports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    ImportMapping,
    ImportSummary,
    ItemStatus,
    QueueItem,
)
from tt_auto_bag_scanner.infrastructure.carrier_config import (
    CarrierConfigError,
    CarrierConfigService,
)
from tt_auto_bag_scanner.infrastructure.excel_parser import (
    ExcelParser,
    SourceRow,
)

# Exact text time formats of the standard outbound report (month/day/4-digit year).
_STANDARD_TIME_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")

# Exact header names of the standard outbound report and the generated template.
STANDARD_COLUMN_HEADERS: Mapping[str, tuple[str, ...]] = {
    "tracking": ("物流运单号", "tracking"),
    "carrier": ("快递公司", "carrier"),
    "status": ("包裹状态", "status"),
    "outbound_at": ("出库时间", "outbound_at"),
    "sort_code": ("Sort Code", "sort_code"),
}


def suggest_column_mapping(
    headers: Iterable[str],
    *,
    saved: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Preselect columns: a still-present saved choice, else one exact standard header.

    Matching is exact after trimming; ambiguous (repeated) headers are not selected.
    The user still reviews the mapping before a preview is built.
    """
    header_list = [str(header) for header in headers]
    present = set(header_list)
    by_name: dict[str, list[str]] = defaultdict(list)
    for header in header_list:
        by_name[header.strip()].append(header)
    mapping: dict[str, str] = {}
    for key, names in STANDARD_COLUMN_HEADERS.items():
        saved_header = (saved or {}).get(key)
        if isinstance(saved_header, str) and saved_header and saved_header in present:
            mapping[key] = saved_header
            continue
        matches = [header for name in names for header in by_name.get(name, ())]
        if len(matches) == 1:
            mapping[key] = matches[0]
    return mapping


@dataclass(frozen=True, slots=True)
class ImportIssue:
    source_row_number: int | None
    field: str
    reason: str
    value: str = ""


@dataclass(frozen=True, slots=True)
class ExcludedRow:
    source_row_number: int
    tracking: str
    reason: str
    sort_code_choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportConflict:
    tracking: str
    source_row_numbers: tuple[int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ImportPreview:
    summary: ImportSummary
    sheet_name: str
    candidate_items: tuple[QueueItem, ...]
    excluded_rows: tuple[ExcludedRow, ...]
    conflicts: tuple[ImportConflict, ...]
    issues: tuple[ImportIssue, ...]
    source_rows: tuple[SourceRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_items", tuple(self.candidate_items))
        object.__setattr__(self, "excluded_rows", tuple(self.excluded_rows))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "source_rows", tuple(self.source_rows))


@dataclass(frozen=True, slots=True)
class _RowCandidate:
    source: SourceRow
    tracking: str
    carrier_value: str
    status_value: str
    outbound_at_utc: datetime | None
    config: CarrierConfig | None
    reported_sort_code: str
    sort_code_choices: tuple[str, ...]
    failure_reason: str | None

    @property
    def signature(self) -> tuple[str, str, str, str, str]:
        if self.config is None:
            target = ("unmapped", CarrierConfigService._normalize(self.carrier_value), "", "")
        else:
            target = (
                self.config.carrier_key,
                self.config.sort_code,
                self.config.container_no,
                "",
            )
        return (*target, self.status_value.strip())


class ImportService:
    def __init__(
        self,
        parser: ExcelParser | None = None,
        carrier_configs: CarrierConfigService | None = None,
    ) -> None:
        self._parser = parser or ExcelParser()
        self._carrier_configs = carrier_configs or CarrierConfigService()

    def build_preview(
        self,
        source: str | Path,
        mapping: ImportMapping,
        carrier_configs: Sequence[CarrierConfig],
        as_of_utc: datetime,
        *,
        task_id: str,
        sheet_name: str | None = None,
        header_row: int = 1,
    ) -> ImportPreview:
        if not task_id.strip():
            raise ValueError("task_id is required")
        if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
            raise ValueError("as_of_utc must include a timezone")
        if mapping.threshold_minutes < 0:
            raise ValueError("threshold_minutes cannot be negative")
        as_of = as_of_utc.astimezone(UTC)
        workbook = self._parser.parse(
            source,
            sheet_name=sheet_name,
            header_row=header_row,
            tracking_column=mapping.tracking_column,
        )
        issues: list[ImportIssue] = []
        excluded: list[ExcludedRow] = []
        conflicts: list[ImportConflict] = []
        sort_code_overrides = dict(mapping.sort_code_overrides)
        headers = set(workbook.headers)

        sort_column = getattr(mapping, "sort_code_column", None)
        mapped_columns = {
            "tracking": mapping.tracking_column.strip(),
            "carrier": mapping.carrier_column.strip(),
            "status": mapping.status_column.strip(),
            "outbound_at": mapping.outbound_at_column.strip(),
        }
        if sort_column:
            mapped_columns["sort_code"] = sort_column.strip()
        missing_columns = {
            key: column
            for key, column in mapped_columns.items()
            if not column or column not in headers
        }
        for field_name, column in missing_columns.items():
            issues.append(ImportIssue(None, field_name, "mapped_column_missing", column))

        timezone: ZoneInfo | None = None
        timezone_name = mapping.report_timezone.strip()
        if not timezone_name:
            issues.append(ImportIssue(None, "report_timezone", "timezone_unconfirmed"))
        else:
            try:
                timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                issues.append(
                    ImportIssue(None, "report_timezone", "timezone_invalid", timezone_name)
                )

        self._carrier_configs.validate(carrier_configs)
        descriptors: list[_RowCandidate] = []
        source_row_numbers = {row.row_number for row in workbook.rows}
        for source_row_number in sorted(set(sort_code_overrides) - source_row_numbers):
            issues.append(
                ImportIssue(
                    source_row_number,
                    "sort_code",
                    "sort_code_override_row_not_found",
                    str(source_row_number),
                )
            )
        for source_row in workbook.rows:
            row_issues = [
                ImportIssue(source_row.row_number, issue.field, issue.reason, issue.value)
                for issue in source_row.issues
            ]
            issues.extend(row_issues)
            values = source_row.values
            tracking = self._text_value(values.get(mapped_columns["tracking"], ""))
            carrier_value = self._text_value(values.get(mapped_columns["carrier"], ""))
            status_value = self._text_value(values.get(mapped_columns["status"], ""))
            time_value = values.get(mapped_columns["outbound_at"])
            sort_value = (
                self._text_value(values.get(mapped_columns["sort_code"], ""))
                if "sort_code" in mapped_columns
                else ""
            )

            failure_reason: str | None = None
            if source_row.issues:
                failure_reason = source_row.issues[0].reason
            elif missing_columns:
                failure_reason = "mapped_column_missing"
            elif not tracking:
                failure_reason = "tracking_missing"

            config: CarrierConfig | None = None
            sort_code_choices: tuple[str, ...] = ()
            row_override = sort_code_overrides.get(source_row.row_number)
            if failure_reason is None:
                try:
                    config = self._carrier_configs.resolve(
                        carrier_value,
                        carrier_configs,
                        reported_sort_code=sort_value or None,
                    )
                    if row_override is not None:
                        if sort_value and self._carrier_configs._normalize(sort_value) != (
                            self._carrier_configs._normalize(row_override)
                        ):
                            failure_reason = "sort_code_override_conflicts_with_report"
                            config = None
                        elif self._carrier_configs._normalize(config.sort_code) != (
                            self._carrier_configs._normalize(row_override)
                        ):
                            failure_reason = "invalid_sort_code_override"
                            config = None
                except CarrierConfigError as error:
                    message = str(error)
                    if "reported sort code is not mapped" in message:
                        failure_reason = "reported_sort_code_unmapped"
                    elif "disabled" in message:
                        failure_reason = "carrier_disabled"
                    elif "ambiguous" in message:
                        failure_reason = "ambiguous_carrier_mapping"
                    elif "missing" in message:
                        failure_reason = "carrier_missing"
                    else:
                        failure_reason = "unmapped_carrier"
                    if failure_reason == "ambiguous_carrier_mapping":
                        sort_code_choices = self._candidate_sort_codes(
                            carrier_value,
                            carrier_configs,
                        )
                        if row_override is not None:
                            selected_code = next(
                                (
                                    choice
                                    for choice in sort_code_choices
                                    if self._carrier_configs._normalize(choice)
                                    == self._carrier_configs._normalize(row_override)
                                ),
                                None,
                            )
                            if selected_code is None:
                                failure_reason = "invalid_sort_code_override"
                            else:
                                config = self._carrier_configs.resolve(
                                    carrier_value,
                                    carrier_configs,
                                    reported_sort_code=selected_code,
                                )
                                failure_reason = None
                    if failure_reason is not None:
                        issues.append(
                            ImportIssue(
                                source_row.row_number,
                                "carrier",
                                failure_reason,
                                carrier_value,
                            )
                        )
                if row_override is not None and failure_reason == "invalid_sort_code_override":
                    issues.append(
                        ImportIssue(
                            source_row.row_number,
                            "sort_code",
                            "invalid_sort_code_override",
                            row_override,
                        )
                    )

            outbound_at_utc: datetime | None = None
            time_reason: str | None = None
            if timezone is None:
                time_reason = "timezone_unconfirmed" if not timezone_name else "timezone_invalid"
            elif failure_reason not in {"mapped_column_missing"}:
                outbound_at_utc, time_reason = self._to_utc(time_value, timezone)
            if time_reason is not None:
                issues.append(
                    ImportIssue(
                        source_row.row_number,
                        "outbound_at",
                        time_reason,
                        self._text_value(time_value),
                    )
                )
                failure_reason = failure_reason or time_reason
            if outbound_at_utc is not None and outbound_at_utc > as_of:
                failure_reason = failure_reason or "future_time"
                issues.append(
                    ImportIssue(
                        source_row.row_number,
                        "outbound_at",
                        "future_time",
                        self._text_value(time_value),
                    )
                )

            if not carrier_value:
                failure_reason = failure_reason or "carrier_missing"
                issues.append(ImportIssue(source_row.row_number, "carrier", "carrier_missing"))
            descriptors.append(
                _RowCandidate(
                    source=source_row,
                    tracking=tracking,
                    carrier_value=carrier_value,
                    status_value=status_value,
                    outbound_at_utc=outbound_at_utc,
                    config=config,
                    reported_sort_code=sort_value,
                    sort_code_choices=sort_code_choices,
                    failure_reason=failure_reason,
                )
            )

        groups: dict[str, list[_RowCandidate]] = defaultdict(list)
        for row in descriptors:
            if not row.tracking:
                excluded.append(
                    ExcludedRow(row.source.row_number, "", row.failure_reason or "tracking_missing")
                )
                if row.failure_reason is None:
                    issues.append(
                        ImportIssue(row.source.row_number, "tracking", "tracking_missing")
                    )
                continue
            groups[row.tracking].append(row)

        candidate_items: list[QueueItem] = []
        for tracking, rows in groups.items():
            row_numbers = tuple(sorted(row.source.row_number for row in rows))
            signatures = {row.signature for row in rows}
            if len(rows) > 1 and len(signatures) > 1:
                reason = "duplicate_conflict"
                conflicts.append(ImportConflict(tracking, row_numbers, reason))
                for row in rows:
                    excluded.append(ExcludedRow(row.source.row_number, tracking, reason))
                    issues.append(ImportIssue(row.source.row_number, "tracking", reason, tracking))
                continue

            failed_rows = [row for row in rows if row.failure_reason is not None]
            if failed_rows:
                failure_reasons = {row.failure_reason for row in failed_rows}
                reason = (
                    next(iter(failure_reasons))
                    if len(rows) == 1 or len(failure_reasons) == 1
                    else "duplicate_requires_review"
                )
                if len(rows) > 1 and reason in {
                    "future_time",
                    "missing_time",
                    "invalid_time",
                    "ambiguous_local_time",
                    "nonexistent_local_time",
                }:
                    reason = "duplicate_requires_review"
                for row in rows:
                    choices = (
                        row.sort_code_choices
                        if reason
                        in {"ambiguous_carrier_mapping", "invalid_sort_code_override"}
                        else ()
                    )
                    excluded.append(
                        ExcludedRow(row.source.row_number, tracking, reason, choices)
                    )
                    if not any(
                        issue.source_row_number == row.source.row_number and issue.reason == reason
                        for issue in issues
                    ):
                        issues.append(ImportIssue(row.source.row_number, "row", reason, tracking))
                continue

            status_values = {row.status_value.strip() for row in rows}
            if status_values != {"复核完成"}:
                for row in rows:
                    excluded.append(
                        ExcludedRow(row.source.row_number, tracking, "status_not_exact")
                    )
                    issues.append(
                        ImportIssue(
                            row.source.row_number,
                            "status",
                            "status_not_exact",
                            row.status_value,
                        )
                    )
                continue

            times = [row.outbound_at_utc for row in rows if row.outbound_at_utc is not None]
            if len(times) != len(rows):
                for row in rows:
                    excluded.append(ExcludedRow(row.source.row_number, tracking, "missing_time"))
                continue
            earliest = min(times)
            if earliest > as_of:
                for row in rows:
                    excluded.append(ExcludedRow(row.source.row_number, tracking, "future_time"))
                continue
            age_seconds = (as_of - earliest).total_seconds()
            threshold_seconds = mapping.threshold_minutes * 60
            if age_seconds < threshold_seconds:
                for row in rows:
                    excluded.append(ExcludedRow(row.source.row_number, tracking, "too_recent"))
                continue

            config = rows[0].config
            if config is None:
                for row in rows:
                    excluded.append(
                        ExcludedRow(row.source.row_number, tracking, "unmapped_carrier")
                    )
                continue
            candidate_items.append(
                QueueItem(
                    id=str(uuid5(NAMESPACE_URL, f"tt-auto-bag-scanner:{task_id}:{tracking}")),
                    task_id=task_id,
                    tracking=tracking,
                    carrier_key=config.carrier_key,
                    sort_code=config.sort_code,
                    container_no=config.container_no,
                    outbound_at_utc=earliest,
                    source_row_numbers=row_numbers,
                    status=ItemStatus.WAIT_PHYSICAL,
                    source_hash=workbook.source_hash,
                    created_at_utc=as_of,
                )
            )

        candidates_sorted = tuple(
            sorted(
                candidate_items,
                key=lambda item: (
                    item.carrier_key.casefold(),
                    item.sort_code.casefold(),
                    item.container_no.casefold(),
                    item.outbound_at_utc,
                    item.source_row_numbers[0],
                ),
            )
        )
        excluded_sorted = tuple(
            sorted(excluded, key=lambda row: (row.source_row_number, row.tracking, row.reason))
        )
        conflicts_sorted = tuple(
            sorted(conflicts, key=lambda conflict: conflict.source_row_numbers)
        )
        issues_sorted = tuple(
            sorted(
                issues,
                key=lambda issue: (
                    issue.source_row_number is not None,
                    issue.source_row_number or 0,
                    issue.field,
                    issue.reason,
                ),
            )
        )
        summary = ImportSummary(
            source_hash=workbook.source_hash,
            source_name=workbook.source_name,
            total_rows=len(workbook.rows),
            eligible_rows=len(candidates_sorted),
            excluded_rows=len(excluded_sorted),
            conflict_count=len(conflicts_sorted),
            mapping=mapping,
            created_at_utc=workbook.imported_at_utc,
        )
        return ImportPreview(
            summary=summary,
            sheet_name=workbook.sheet_name,
            candidate_items=candidates_sorted,
            excluded_rows=excluded_sorted,
            conflicts=conflicts_sorted,
            issues=issues_sorted,
            source_rows=workbook.rows,
        )

    @staticmethod
    def _text_value(value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _candidate_sort_codes(
        self,
        carrier_value: str,
        carrier_configs: Sequence[CarrierConfig],
    ) -> tuple[str, ...]:
        needle = self._carrier_configs._normalize(carrier_value)
        matching_codes = {
            config.sort_code
            for config in carrier_configs
            if config.enabled
            and needle
            in {
                self._carrier_configs._normalize(config.carrier_key),
                self._carrier_configs._normalize(config.display_name),
                *(self._carrier_configs._normalize(alias) for alias in config.aliases),
            }
        }
        eligible: list[str] = []
        for code in sorted(matching_codes, key=lambda value: (value.casefold(), value)):
            try:
                self._carrier_configs.resolve(
                    carrier_value,
                    carrier_configs,
                    reported_sort_code=code,
                )
            except CarrierConfigError:
                continue
            eligible.append(code)
        return tuple(eligible)

    @staticmethod
    def _to_utc(value: object, timezone: ZoneInfo) -> tuple[datetime | None, str | None]:
        if value is None or value == "":
            return None, "missing_time"
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None, "missing_time"
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
                # The standard outbound report exports US-style local time text.
                for time_format in _STANDARD_TIME_FORMATS:
                    try:
                        parsed = datetime.strptime(text, time_format)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None, "invalid_time"
        else:
            return None, "invalid_time"

        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC), None

        valid_utc: set[datetime] = set()
        for fold in (0, 1):
            local = parsed.replace(tzinfo=timezone, fold=fold)
            utc = local.astimezone(UTC)
            roundtrip = utc.astimezone(timezone).replace(tzinfo=None)
            if roundtrip == parsed:
                valid_utc.add(utc)
        if not valid_utc:
            return None, "nonexistent_local_time"
        if len(valid_utc) > 1:
            return None, "ambiguous_local_time"
        return next(iter(valid_utc)), None
