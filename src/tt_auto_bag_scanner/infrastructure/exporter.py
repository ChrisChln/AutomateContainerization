"""CSV/XLSX report exporters with text-safe identifiers and state totals."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from tt_auto_bag_scanner.domain.models import ItemStatus, TaskCounts

DEFAULT_FIELDS = (
    "tracking",
    "carrier",
    "sort",
    "container",
    "source_rows",
    "outbound_at_utc",
    "attempt_started_at_utc",
    "result_at_utc",
    "status",
    "attempt_id",
    "logid",
    "operator",
    "physical_check_operator",
    "physical_evidence_ref",
    "physical_check_revoked_at_utc",
)


class ReportExporter:
    def export_csv(
        self,
        path: str | Path,
        rows: Iterable[Mapping[str, object]],
        *,
        fieldnames: Sequence[str] | None = None,
    ) -> Path:
        output = Path(path)
        materialized, headers = self._normalize_rows(rows, fieldnames)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in materialized:
                writer.writerow({key: self._csv_safe(value) for key, value in row.items()})
        return output

    def export_xlsx(
        self,
        path: str | Path,
        rows: Iterable[Mapping[str, object]],
        *,
        fieldnames: Sequence[str] | None = None,
    ) -> Path:
        output = Path(path)
        materialized, headers = self._normalize_rows(rows, fieldnames)
        output.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Report")
        sheet.append(list(headers))
        for row in materialized:
            cells = []
            for name in headers:
                value = row.get(name, "")
                safe_value = self._xlsx_safe(value)
                cell = WriteOnlyCell(sheet, value=safe_value)
                if name.casefold() in {"tracking", "tracking_number", "运单号", "物流运单号"}:
                    cell.number_format = "@"
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        workbook.save(output)
        return output

    @staticmethod
    def counts(statuses: Iterable[ItemStatus]) -> TaskCounts:
        return TaskCounts.from_statuses(tuple(statuses))

    @staticmethod
    def _normalize_rows(
        rows: Iterable[Mapping[str, object]], fieldnames: Sequence[str] | None
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        materialized = [dict(row) for row in rows]
        if fieldnames is not None:
            headers = tuple(str(name) for name in fieldnames)
        elif materialized:
            headers = tuple(str(name) for name in materialized[0].keys())
        else:
            headers = DEFAULT_FIELDS
        if not headers or len(headers) != len(set(headers)):
            raise ValueError("report fields must be non-empty and unique")
        return materialized, headers

    @staticmethod
    def _csv_safe(value: object) -> str:
        text = "" if value is None else str(value)
        inspected = text.lstrip(" \t\r\n")
        if inspected.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return f"'{text}"
        return text

    @staticmethod
    def _xlsx_safe(value: object) -> object:
        if not isinstance(value, str):
            return value
        inspected = value.lstrip(" \t\r\n")
        if inspected.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
            return f"'{value}"
        return value
