import re
from hashlib import sha256
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from tt_auto_bag_scanner.infrastructure.excel_parser import (
    ExcelParser,
    ImportFileError,
    WorkbookValidationError,
)


def write_xlsx(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Outbound"
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def set_sheet_dimension_to_single_cell(path):
    with ZipFile(path) as source:
        entries = [(item, source.read(item.filename)) for item in source.infolist()]

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as target:
        for item, content in entries:
            if item.filename == "xl/worksheets/sheet1.xml":
                content, replacements = re.subn(
                    rb'<dimension ref="[^"]+"\s*/>',
                    b'<dimension ref="A1"/>',
                    content,
                    count=1,
                )
                assert replacements == 1
            target.writestr(item, content)


def test_parser_preserves_tracking_text_hash_and_source_rows(tmp_path):
    path = tmp_path / "outbound.xlsx"
    write_xlsx(path, [["tracking", "status"], ["00000000000000123456", "复核完成"]])
    original_hash = sha256(path.read_bytes()).hexdigest()

    parsed = ExcelParser().parse(path, sheet_name="Outbound", header_row=1)

    assert parsed.source_hash == original_hash
    assert parsed.rows[0].values["tracking"] == "00000000000000123456"
    assert parsed.rows[0].row_number == 2
    assert sha256(path.read_bytes()).hexdigest() == original_hash


def test_parser_lists_sheets_and_reads_selected_header_row(tmp_path):
    path = tmp_path / "multi-sheet.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "Summary"
    first.append(["not a report"])
    outbound = workbook.create_sheet("Outbound")
    outbound.append(["report title"])
    outbound.append(["tracking", "carrier", "status"])
    workbook.save(path)

    parser = ExcelParser()

    assert parser.list_sheet_names(path) == ("Summary", "Outbound")
    assert parser.read_headers(path, sheet_name="Outbound", header_row=2) == (
        "tracking",
        "carrier",
        "status",
    )


def test_parser_reads_data_when_worksheet_dimension_is_too_small(tmp_path):
    path = tmp_path / "incorrect-dimension.xlsx"
    write_xlsx(
        path,
        [
            ["tracking", "carrier", "status"],
            ["SYN-001", "CARRIER-SYN", "Ready"],
        ],
    )
    set_sheet_dimension_to_single_cell(path)

    parser = ExcelParser()

    assert parser.read_headers(path, sheet_name="Outbound") == (
        "tracking",
        "carrier",
        "status",
    )
    parsed = parser.parse(path, sheet_name="Outbound")

    assert len(parsed.rows) == 1
    assert parsed.rows[0].values == {
        "tracking": "SYN-001",
        "carrier": "CARRIER-SYN",
        "status": "Ready",
    }


def test_parser_rejects_duplicate_headers_with_readable_error(tmp_path):
    path = tmp_path / "duplicate.xlsx"
    write_xlsx(path, [["tracking", " tracking "], ["A", "B"]])

    with pytest.raises(WorkbookValidationError, match="duplicate header"):
        ExcelParser().parse(path)


def test_parser_marks_formula_without_cached_value_as_row_issue(tmp_path):
    path = tmp_path / "formula.xlsx"
    write_xlsx(path, [["tracking", "status"], ["=1+1", "复核完成"]])

    parsed = ExcelParser().parse(path)

    assert any(
        issue.field == "tracking" and "formula" in issue.reason for issue in parsed.rows[0].issues
    )


def test_parser_rejects_numeric_tracking_instead_of_guessing_text(tmp_path):
    path = tmp_path / "numeric.xlsx"
    write_xlsx(path, [["tracking", "status"], [1234567890123456, "复核完成"]])

    parsed = ExcelParser().parse(path)

    assert any(
        issue.field == "tracking" and "text" in issue.reason for issue in parsed.rows[0].issues
    )


def test_parser_checks_the_explicit_tracking_column_name(tmp_path):
    path = tmp_path / "mapped-numeric.xlsx"
    write_xlsx(path, [["Waybill ID", "status"], [1234567890123, "复核完成"]])

    parsed = ExcelParser().parse(path, tracking_column="Waybill ID")

    assert any(issue.field == "Waybill ID" for issue in parsed.rows[0].issues)


def test_parser_rejects_non_xlsx_and_corrupt_workbook(tmp_path):
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("tracking,status\nA,复核完成", encoding="utf-8")
    broken_path = tmp_path / "broken.xlsx"
    broken_path.write_bytes(b"not an xlsx archive")

    with pytest.raises(ImportFileError, match=r"\.xlsx"):
        ExcelParser().parse(csv_path)
    with pytest.raises(ImportFileError, match="readable"):
        ExcelParser().parse(broken_path)
