import csv
from pathlib import Path

from openpyxl import load_workbook

from tt_auto_bag_scanner.domain.models import ItemStatus, TaskCounts
from tt_auto_bag_scanner.infrastructure.exporter import ReportExporter


def test_csv_export_neutralizes_formula_cells_and_preserves_tracking_text(tmp_path):
    path = tmp_path / "report.csv"
    ReportExporter().export_csv(
        path,
        [
            {
                "tracking": "00000000000000123456",
                "carrier": '=HYPERLINK("https://example.invalid","x")',
                "container": "@danger",
            }
        ],
    )

    with path.open(encoding="utf-8-sig", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["tracking"] == "00000000000000123456"
    assert row["carrier"].startswith("'")
    assert row["container"].startswith("'")


def test_xlsx_export_stores_tracking_as_text_and_counts_duplicate_separately(tmp_path):
    exporter = ReportExporter()
    path = exporter.export_xlsx(
        Path(tmp_path / "synthetic-export.xlsx"),
        [{"tracking": "00000000000000123456", "status": "ALREADY_IN_BAG"}],
    )
    try:
        workbook = load_workbook(path, data_only=True)
        sheet = workbook.active
        assert sheet["A2"].value == "00000000000000123456"
        assert sheet["A2"].number_format == "@"
    finally:
        path.unlink(missing_ok=True)

    counts = TaskCounts.from_statuses(
        [ItemStatus.SUCCESS, ItemStatus.ALREADY_IN_BAG, ItemStatus.UNKNOWN]
    )
    assert counts.confirmed_complete == 2
    assert counts.by_status[ItemStatus.SUCCESS] == 1
    assert counts.by_status[ItemStatus.ALREADY_IN_BAG] == 1
    assert counts.unresolved == 1
