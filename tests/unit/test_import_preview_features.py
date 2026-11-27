from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication

from tt_auto_bag_scanner.application.import_service import (
    ExcludedRow,
    ImportPreview,
    ImportService,
)
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    ImportMapping,
    ImportSummary,
    ItemStatus,
    QueueItem,
)
from tt_auto_bag_scanner.ui.main_window import ExcludedRowsDialog, MainWindow


def make_config(sort_code: str, container: str) -> CarrierConfig:
    return CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Carrier Synthetic",
        aliases=("Synthetic Alias",),
        sort_code=sort_code,
        container_no=container,
        enabled=True,
        version=1,
        updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_import_mapping(**overrides: object) -> ImportMapping:
    return ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone="UTC",
        threshold_minutes=30,
        **overrides,
    )


def write_ambiguous_report(path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Status", "Outbound At"])
    sheet.append(
        [
            "TRACKING-SYNTHETIC-01",
            "Synthetic Alias",
            "复核完成",
            datetime(2026, 11, 26, 17, 0),
        ]
    )
    workbook.save(path)


def test_row_sort_override_resolves_only_the_selected_eligible_mapping_and_persists(
    tmp_path,
):
    path = tmp_path / "ambiguous-synthetic.xlsx"
    write_ambiguous_report(path)
    configs = [make_config("SORT-SYNTHETIC-A", "CONTAINER-SYNTHETIC-A"),
               make_config("SORT-SYNTHETIC-B", "CONTAINER-SYNTHETIC-B")]
    service = ImportService()
    as_of = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)

    unresolved = service.build_preview(
        path,
        make_import_mapping(),
        configs,
        as_of,
        task_id="task-synthetic-unresolved",
    )
    assert unresolved.excluded_rows[0].reason == "ambiguous_carrier_mapping"
    assert unresolved.excluded_rows[0].sort_code_choices == (
        "SORT-SYNTHETIC-A",
        "SORT-SYNTHETIC-B",
    )

    mapping = make_import_mapping(sort_code_overrides={2: "SORT-SYNTHETIC-B"})
    resolved = service.build_preview(
        path,
        mapping,
        configs,
        as_of,
        task_id="task-synthetic-resolved",
    )

    assert [(item.sort_code, item.container_no) for item in resolved.candidate_items] == [
        ("SORT-SYNTHETIC-B", "CONTAINER-SYNTHETIC-B")
    ]
    assert resolved.excluded_rows == ()
    assert asdict(resolved.summary.mapping)["sort_code_overrides"] == (
        (2, "SORT-SYNTHETIC-B"),
    )


def test_invalid_or_report_conflicting_override_does_not_queue_ambiguous_row(tmp_path):
    path = tmp_path / "invalid-override-synthetic.xlsx"
    write_ambiguous_report(path)
    configs = [make_config("SORT-SYNTHETIC-A", "CONTAINER-SYNTHETIC-A"),
               make_config("SORT-SYNTHETIC-B", "CONTAINER-SYNTHETIC-B")]

    preview = ImportService().build_preview(
        path,
        make_import_mapping(sort_code_overrides={2: "SORT-NOT-CONFIGURED"}),
        configs,
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic-invalid-override",
    )

    assert preview.candidate_items == ()
    assert preview.excluded_rows[0].reason == "invalid_sort_code_override"
    assert preview.excluded_rows[0].sort_code_choices == (
        "SORT-SYNTHETIC-A",
        "SORT-SYNTHETIC-B",
    )


def test_row_override_cannot_replace_a_reported_sort_code(tmp_path):
    path = tmp_path / "reported-sort-synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Sort", "Status", "Outbound At"])
    sheet.append(
        [
            "TRACKING-SYNTHETIC-01",
            "Synthetic Alias",
            "SORT-SYNTHETIC-A",
            "复核完成",
            datetime(2026, 11, 26, 17, 0),
        ]
    )
    workbook.save(path)
    mapping = make_import_mapping(
        sort_code_column="Sort",
        sort_code_overrides={2: "SORT-SYNTHETIC-B"},
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        [
            make_config("SORT-SYNTHETIC-A", "CONTAINER-SYNTHETIC-A"),
            make_config("SORT-SYNTHETIC-B", "CONTAINER-SYNTHETIC-B"),
        ],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic-reported-sort",
    )

    assert preview.candidate_items == ()
    assert preview.excluded_rows[0].reason == "sort_code_override_conflicts_with_report"


def test_sort_override_does_not_change_an_unrelated_status_exclusion(tmp_path):
    path = tmp_path / "override-status-synthetic.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Sort", "Status", "Outbound At"])
    sheet.append(
        [
            "TRACKING-SYNTHETIC-01",
            "Synthetic Alias",
            None,
            "复核完成",
            datetime(2026, 11, 26, 17, 0),
        ]
    )
    sheet.append(
        [
            "TRACKING-SYNTHETIC-02",
            "Synthetic Alias",
            "SORT-SYNTHETIC-A",
            "待复核",
            datetime(2026, 11, 26, 17, 0),
        ]
    )
    workbook.save(path)
    mapping = make_import_mapping(
        sort_code_column="Sort",
        sort_code_overrides={2: "SORT-SYNTHETIC-B"},
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        [make_config("SORT-SYNTHETIC-A", "CONTAINER-SYNTHETIC-A"),
         make_config("SORT-SYNTHETIC-B", "CONTAINER-SYNTHETIC-B")],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic-unrelated-exclusion",
    )

    assert [item.tracking for item in preview.candidate_items] == ["TRACKING-SYNTHETIC-01"]
    assert [(row.source_row_number, row.reason) for row in preview.excluded_rows] == [
        (3, "status_not_exact")
    ]


def queue_item(tracking: str, carrier: str, row: int) -> QueueItem:
    return QueueItem(
        id=f"item-{tracking}",
        task_id="task-synthetic-ui",
        tracking=tracking,
        carrier_key=carrier,
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        outbound_at_utc=datetime(2026, 11, 26, 17, 0, tzinfo=UTC),
        source_row_numbers=(row,),
        status=ItemStatus.WAIT_PHYSICAL,
    )


def make_preview(items: tuple[QueueItem, ...]) -> ImportPreview:
    mapping = make_import_mapping(sort_code_overrides={4: "SORT-SYNTHETIC-A"})
    summary = ImportSummary(
        source_hash="synthetic-hash",
        source_name="synthetic-report.xlsx",
        total_rows=len(items),
        eligible_rows=len(items),
        excluded_rows=1,
        conflict_count=0,
        mapping=mapping,
        created_at_utc=datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
    )
    return ImportPreview(
        summary=summary,
        sheet_name="Synthetic Sheet",
        candidate_items=items,
        excluded_rows=(
            ExcludedRow(
                source_row_number=8,
                tracking="TRACKING-EXCLUDED",
                reason="ambiguous_carrier_mapping",
                sort_code_choices=("SORT-SYNTHETIC-A", "SORT-SYNTHETIC-B"),
            ),
        ),
        conflicts=(),
        issues=(),
        source_rows=(),
    )


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    instance = QApplication.instance()
    instance = instance if instance is not None else QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    yield instance
    for widget in instance.topLevelWidgets():
        if isinstance(widget, MainWindow):
            widget.close()
    instance.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    instance.processEvents()


def test_preview_search_updates_visible_candidates_and_carrier_counts(app: QApplication):
    window = MainWindow()
    preview = make_preview(
        (
            queue_item("TRACKING-SYNTHETIC-01", "CARRIER-A", 2),
            queue_item("OTHER-SYNTHETIC-02", "CARRIER-B", 3),
        )
    )
    window.set_import_preview(preview)

    window.candidate_search_edit.setText("tracking-synthetic")

    assert window.queue_table.rowCount() == 1
    assert window.queue_table.item(0, 1).text() == "TRACKING-SYNTHETIC-01"
    assert window.carrier_candidate_counts_label.text() == "渠道候选：CARRIER-A 1"
    window.close()


def test_preview_export_signal_sends_candidates_and_exclusions(app: QApplication):
    window = MainWindow()
    preview = make_preview((queue_item("TRACKING-SYNTHETIC-01", "CARRIER-A", 2),))
    emitted: list[object] = []
    window.preview_export_requested.connect(emitted.append)
    window.set_import_preview(preview)

    window.preview_export_button.click()

    assert emitted == [preview]
    window.close()


def test_excluded_rows_dialog_returns_only_explicit_sort_choices(app: QApplication):
    preview = make_preview(())
    dialog = ExcludedRowsDialog(
        preview.excluded_rows,
        selected_overrides=dict(preview.summary.mapping.sort_code_overrides),
    )

    assert dialog.apply_button.isEnabled() is False
    dialog.choice_combos[8].setCurrentIndex(2)

    assert dialog.apply_button.isEnabled() is True
    assert dialog.selected_sort_overrides == {8: "SORT-SYNTHETIC-B"}
    dialog.close()


def test_main_window_requests_rebuild_with_selected_and_saved_overrides(app: QApplication):
    window = MainWindow()
    preview = make_preview(())
    emitted: list[object] = []
    window.sort_code_overrides_requested.connect(emitted.append)
    window.set_import_preview(preview)

    def choose_sort_code() -> None:
        dialog = next(
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, ExcludedRowsDialog)
        )
        dialog.choice_combos[8].setCurrentIndex(2)
        dialog.apply_button.click()

    QTimer.singleShot(0, choose_sort_code)
    window._show_excluded_rows()

    assert emitted == [{4: "SORT-SYNTHETIC-A", 8: "SORT-SYNTHETIC-B"}]
    window.close()


def test_status_summary_includes_every_item_state_and_attempted_progress(app: QApplication):
    window = MainWindow()
    counts = {
        status.value: index
        for index, status in enumerate(ItemStatus, start=1)
    }

    window.set_counts(counts=counts, attempted=4, total=sum(counts.values()))

    assert "待核验 1" in window.status_counts_first_label.text()
    assert "已取消 8" in window.status_counts_second_label.text()
    assert window.task_attempted_label.text() == f"已尝试 4 / {sum(counts.values())}"
    window.close()


def test_reconciliation_actions_are_disabled_while_worker_runs_and_restored_after(
    app: QApplication,
):
    window = MainWindow()
    item = replace(queue_item("TRACKING-UNKNOWN", "CARRIER-A", 2), status=ItemStatus.UNKNOWN)
    window.set_queue_items([item])
    window.workspace_table.selectRow(0)
    window.set_unresolved_rows(
        [{"tracking": item.tracking, "carrier": item.carrier_key, "item_id": item.id}]
    )
    window.unresolved_table.selectRow(0)

    assert window.review_result_button.isEnabled() is True
    assert window.open_reconciliation_button.isEnabled() is True
    window.set_running(True)
    assert window.review_result_button.isEnabled() is False
    assert window.open_reconciliation_button.isEnabled() is False

    window.set_running(False)

    assert window.review_result_button.isEnabled() is True
    assert window.open_reconciliation_button.isEnabled() is True
    window.close()
