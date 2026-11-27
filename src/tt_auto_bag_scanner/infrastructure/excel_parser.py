"""Read-only XLSX inspection with source-row and integrity metadata."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException


class ImportFileError(ValueError):
    """The selected file cannot be safely read as an XLSX workbook."""


class WorkbookValidationError(ValueError):
    """The selected workbook structure cannot be mapped unambiguously."""


@dataclass(frozen=True, slots=True)
class RowIssue:
    field: str
    reason: str
    value: str = ""


@dataclass(frozen=True, slots=True)
class SourceRow:
    row_number: int
    values: Mapping[str, object]
    issues: tuple[RowIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True, slots=True)
class ParsedWorkbook:
    source_name: str
    source_hash: str
    sheet_name: str
    headers: tuple[str, ...]
    rows: tuple[SourceRow, ...]
    imported_at_utc: datetime


class ExcelParser:
    """Inspect `.xlsx` files without changing the source workbook."""

    def list_sheet_names(self, path: str | Path) -> tuple[str, ...]:
        source = self._validated_source(path)
        workbook = None
        try:
            workbook = load_workbook(source, read_only=True, data_only=True)
            return tuple(workbook.sheetnames)
        except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError, EOFError) as error:
            raise ImportFileError(
                "The selected workbook is not readable as an .xlsx file"
            ) from error
        finally:
            if workbook is not None:
                workbook.close()

    def read_headers(
        self,
        path: str | Path,
        *,
        sheet_name: str | None = None,
        header_row: int = 1,
    ) -> tuple[str, ...]:
        source = self._validated_source(path)
        if header_row < 1:
            raise WorkbookValidationError("header row must be a positive row number")
        workbook = None
        try:
            workbook = load_workbook(source, read_only=True, data_only=True)
            selected_name = sheet_name or (workbook.sheetnames[0] if workbook.sheetnames else "")
            if selected_name not in workbook.sheetnames:
                raise WorkbookValidationError(f"worksheet not found: {selected_name}")
            worksheet = workbook[selected_name]
            worksheet.reset_dimensions()
            values = next(
                worksheet.iter_rows(
                    min_row=header_row,
                    max_row=header_row,
                    values_only=True,
                ),
                (),
            )
            return self._normalize_headers(values)
        except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError, EOFError) as error:
            if isinstance(error, WorkbookValidationError):
                raise
            raise ImportFileError(
                "The selected workbook is not readable as an .xlsx file"
            ) from error
        finally:
            if workbook is not None:
                workbook.close()

    def parse(
        self,
        path: str | Path,
        *,
        sheet_name: str | None = None,
        header_row: int = 1,
        tracking_column: str | None = None,
    ) -> ParsedWorkbook:
        source = self._validated_source(path)
        if header_row < 1:
            raise WorkbookValidationError("header row must be a positive row number")

        source_hash = self._sha256(source)
        values_book = None
        formulas_book = None
        try:
            values_book = load_workbook(source, read_only=True, data_only=True)
            formulas_book = load_workbook(source, read_only=True, data_only=False)
            if sheet_name is None:
                selected_name = values_book.sheetnames[0] if values_book.sheetnames else ""
            else:
                selected_name = sheet_name
            if selected_name not in values_book.sheetnames:
                raise WorkbookValidationError(f"worksheet not found: {selected_name}")

            value_sheet = values_book[selected_name]
            formula_sheet = formulas_book[selected_name]
            value_sheet.reset_dimensions()
            formula_sheet.reset_dimensions()
            header_values = next(
                value_sheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True),
                (),
            )
            headers = self._normalize_headers(header_values)
            formula_headers = next(
                formula_sheet.iter_rows(min_row=header_row, max_row=header_row, values_only=True),
                (),
            )
            formula_header_names = self._normalize_headers(formula_headers, validate=False)

            data_rows: list[SourceRow] = []
            max_column = max(len(headers), len(formula_header_names))
            value_rows = value_sheet.iter_rows(
                min_row=header_row + 1,
                max_col=max_column,
                values_only=True,
            )
            formula_rows = formula_sheet.iter_rows(
                min_row=header_row + 1,
                max_col=max_column,
                values_only=False,
            )
            for row_number, (values, formula_cells) in enumerate(
                zip(value_rows, formula_rows),
                start=header_row + 1,
            ):
                row_values = tuple(values)
                if not any(value is not None and str(value).strip() for value in row_values):
                    continue

                issues: list[RowIssue] = []
                mapped_values: dict[str, object] = {}
                for column_index, header in enumerate(headers):
                    value = row_values[column_index] if column_index < len(row_values) else None
                    formula_cell = (
                        formula_cells[column_index] if column_index < len(formula_cells) else None
                    )
                    if formula_cell is not None and formula_cell.data_type == "f" and value is None:
                        issues.append(RowIssue(header, "formula_missing_cached_value"))
                    is_tracking_column = (
                        tracking_column is not None and header == tracking_column.strip()
                    ) or header.casefold() in {
                        "tracking",
                        "tracking number",
                        "运单号",
                        "物流运单号",
                    }
                    if is_tracking_column:
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            issues.append(
                                RowIssue(header, "tracking_must_be_text", self._display(value))
                            )
                    mapped_values[header] = value
                data_rows.append(SourceRow(row_number, mapped_values, tuple(issues)))
        except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError, EOFError) as error:
            if isinstance(error, WorkbookValidationError):
                raise
            raise ImportFileError(
                "The selected workbook is not readable as an .xlsx file"
            ) from error
        finally:
            if values_book is not None:
                values_book.close()
            if formulas_book is not None:
                formulas_book.close()

        if self._sha256(source) != source_hash:
            raise ImportFileError("The source workbook changed during import; select it again")

        return ParsedWorkbook(
            source_name=source.name,
            source_hash=source_hash,
            sheet_name=selected_name,
            headers=headers,
            rows=tuple(data_rows),
            imported_at_utc=datetime.now(UTC),
        )

    @staticmethod
    def _validated_source(path: str | Path) -> Path:
        source = Path(path)
        if source.suffix.lower() != ".xlsx":
            raise ImportFileError("Only .xlsx files can be imported")
        if not source.is_file():
            raise ImportFileError("The selected workbook is not readable")
        return source

    @staticmethod
    def _normalize_headers(values, *, validate: bool = True) -> tuple[str, ...]:
        headers = tuple(str(value).strip() if value is not None else "" for value in values)
        non_empty = [header.casefold() for header in headers if header]
        if validate and not any(headers):
            raise WorkbookValidationError("worksheet has no header values")
        if validate and len(non_empty) != len(set(non_empty)):
            raise WorkbookValidationError("duplicate header names are not allowed")
        return headers

    @staticmethod
    def _sha256(source: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _display(value: object) -> str:
        return str(value)
