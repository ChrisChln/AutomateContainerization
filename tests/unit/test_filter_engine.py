from datetime import UTC, datetime

from openpyxl import Workbook

from tt_auto_bag_scanner.application.import_service import ImportService
from tt_auto_bag_scanner.domain.models import CarrierConfig, ImportMapping, ItemStatus


def make_config(
    key="SYNTHETIC_CARRIER", alias="Synthetic Carrier", sort="SORT-A", container="BAG-A"
):
    return CarrierConfig(
        carrier_key=key,
        display_name=alias,
        aliases=(alias,),
        sort_code=sort,
        container_no=container,
        enabled=True,
        version=1,
        updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_report(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Outbound"
    sheet.append(["Tracking", "Carrier", "Status", "Outbound At"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def make_mapping(timezone="UTC", threshold=30):
    return ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone=timezone,
        threshold_minutes=threshold,
    )


def test_threshold_is_inclusive_and_status_match_is_strict_after_trim(tmp_path):
    path = tmp_path / "threshold.xlsx"
    make_report(
        path,
        [
            ["SYN-30", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 30)],
            ["SYN-29", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 30, 1)],
            ["SYN-STATUS", "Synthetic Carrier", "复核完成!", datetime(2026, 11, 26, 17, 0)],
        ],
    )

    preview = ImportService().build_preview(
        path,
        make_mapping(),
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    assert [item.tracking for item in preview.candidate_items] == ["SYN-30"]
    assert preview.sheet_name == "Outbound"
    assert {row.reason for row in preview.excluded_rows} >= {"too_recent", "status_not_exact"}


def test_same_target_duplicates_merge_and_cross_carrier_conflicts_are_isolated(tmp_path):
    path = tmp_path / "duplicates.xlsx"
    make_report(
        path,
        [
            ["SYN-DUP", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 0)],
            ["SYN-DUP", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 5)],
            ["SYN-CONFLICT", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 0)],
            ["SYN-CONFLICT", "Other Carrier", "复核完成", datetime(2026, 11, 26, 17, 1)],
        ],
    )

    preview = ImportService().build_preview(
        path,
        make_mapping(),
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    assert len(preview.candidate_items) == 1
    assert preview.candidate_items[0].tracking == "SYN-DUP"
    assert preview.candidate_items[0].source_row_numbers == (2, 3)
    assert preview.candidate_items[0].outbound_at_utc == datetime(2026, 11, 26, 17, 0, tzinfo=UTC)
    assert preview.conflicts[0].tracking == "SYN-CONFLICT"
    assert preview.conflicts[0].source_row_numbers == (4, 5)


def test_ambiguous_future_and_missing_times_are_review_issues_not_candidates(tmp_path):
    path = tmp_path / "times.xlsx"
    make_report(
        path,
        [
            ["SYN-DST", "Synthetic Carrier", "复核完成", datetime(2026, 11, 1, 1, 30)],
            ["SYN-FUTURE", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 19, 0)],
            ["SYN-MISSING", "Synthetic Carrier", "复核完成", None],
        ],
    )

    preview = ImportService().build_preview(
        path,
        make_mapping("America/New_York"),
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    reasons = {row.reason for row in preview.excluded_rows}
    assert "ambiguous_local_time" in reasons
    assert "future_time" in reasons
    assert "missing_time" in reasons
    assert preview.candidate_items == ()
    assert {issue.reason for issue in preview.issues} >= reasons


def test_preview_requires_an_explicit_timezone_and_returns_deterministic_order(tmp_path):
    path = tmp_path / "order.xlsx"
    make_report(
        path,
        [
            ["SYN-LATER", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 15)],
            ["SYN-EARLIER", "Synthetic Carrier", "复核完成", datetime(2026, 11, 26, 17, 0)],
        ],
    )

    service = ImportService()
    as_of = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    with_timezone = service.build_preview(
        path, make_mapping(), [make_config()], as_of, task_id="task-synthetic"
    )
    without_timezone = service.build_preview(
        path,
        make_mapping(""),
        [make_config()],
        as_of,
        task_id="task-synthetic",
    )

    assert [item.tracking for item in with_timezone.candidate_items] == ["SYN-EARLIER", "SYN-LATER"]
    assert [item.tracking for item in without_timezone.candidate_items] == []
    assert any(issue.field == "report_timezone" for issue in without_timezone.issues)
    assert all(item.status is ItemStatus.WAIT_PHYSICAL for item in with_timezone.candidate_items)


def test_multi_sort_mapping_requires_an_explicit_matching_report_sort(tmp_path):
    path = tmp_path / "sorts.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Sort", "Status", "Outbound At"])
    sheet.append(
        ["SYN-SORT", "Synthetic Carrier", "SORT-B", "复核完成", datetime(2026, 11, 26, 17, 0)]
    )
    workbook.save(path)
    configs = [
        make_config(sort="SORT-A", container="BAG-A"),
        make_config(sort="SORT-B", container="BAG-B"),
    ]
    mapping = ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone="UTC",
        threshold_minutes=30,
        sort_code_column="Sort",
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        configs,
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    assert [(item.sort_code, item.container_no) for item in preview.candidate_items] == [
        ("SORT-B", "BAG-B")
    ]


def test_invalid_local_dst_time_is_never_queued(tmp_path):
    path = tmp_path / "dst-gap.xlsx"
    make_report(
        path,
        [["SYN-GAP", "Synthetic Carrier", "复核完成", datetime(2026, 3, 8, 2, 30)]],
    )

    preview = ImportService().build_preview(
        path,
        make_mapping("America/New_York"),
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    assert preview.candidate_items == ()
    assert preview.excluded_rows[0].reason == "nonexistent_local_time"


def test_formula_without_cache_in_optional_sort_column_is_not_queued(tmp_path):
    path = tmp_path / "formula-sort.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Sort", "Status", "Outbound At"])
    sheet.append(
        ["SYN-FORMULA", "Synthetic Carrier", "=1+1", "复核完成", datetime(2026, 11, 26, 17, 0)]
    )
    workbook.save(path)
    configs = [make_config()]
    mapping = ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone="UTC",
        threshold_minutes=30,
        sort_code_column="Sort",
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        configs,
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-synthetic",
    )

    assert preview.candidate_items == ()
    assert preview.excluded_rows[0].reason == "formula_missing_cached_value"
    assert any(issue.field == "Sort" for issue in preview.issues)


def test_preview_lists_unconfigured_carrier_sort_pairs_from_report(tmp_path):
    path = tmp_path / "pairs.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Status", "Outbound At", "Sort"])
    when = datetime(2026, 11, 26, 17, 0)
    for row in (
        ["SYN-1", "Carrier A", "复核完成", when, "SORT-A1"],
        ["SYN-2", "carrier a ", "复核完成", when, "sort-a1"],
        ["SYN-3", "Carrier B", "复核完成", when, "SORT-B1"],
        ["SYN-4", "Synthetic Carrier", "复核完成", when, "SORT-A"],
        ["SYN-5", "Carrier C", "复核完成", when, ""],
    ):
        sheet.append(row)
    workbook.save(path)
    mapping = ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone="UTC",
        sort_code_column="Sort",
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-pairs",
    )

    assert preview.unconfigured_carriers == (
        ("Carrier A", "SORT-A1"),
        ("Carrier B", "SORT-B1"),
    )


def test_eligible_rows_without_container_are_previewed_but_not_queued(tmp_path):
    path = tmp_path / "awaiting.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tracking", "Carrier", "Status", "Outbound At", "Sort"])
    old = datetime(2026, 11, 26, 17, 0)
    for row in (
        ["SYN-WAIT", "Carrier A", "复核完成", old, "SORT-A1"],
        ["SYN-RECENT", "Carrier A", "复核完成", datetime(2026, 11, 26, 17, 59), "SORT-A1"],
        ["SYN-STATUS", "Carrier A", "已出库", old, "SORT-A1"],
        ["SYN-NOSORT", "Carrier C", "复核完成", old, ""],
        ["SYN-READY", "Synthetic Carrier", "复核完成", old, "SORT-A"],
    ):
        sheet.append(row)
    workbook.save(path)
    mapping = ImportMapping(
        tracking_column="Tracking",
        carrier_column="Carrier",
        status_column="Status",
        outbound_at_column="Outbound At",
        report_timezone="UTC",
        sort_code_column="Sort",
    )

    preview = ImportService().build_preview(
        path,
        mapping,
        [make_config()],
        datetime(2026, 11, 26, 18, 0, tzinfo=UTC),
        task_id="task-awaiting",
    )

    assert [item.tracking for item in preview.candidate_items] == ["SYN-READY"]
    assert [
        (row.tracking, row.carrier_key, row.sort_code, row.container_no)
        for row in preview.awaiting_container
    ] == [("SYN-WAIT", "Carrier A", "SORT-A1", "")]
    reasons = {row.tracking: row.reason for row in preview.excluded_rows}
    assert reasons == {
        "SYN-WAIT": "awaiting_container",
        "SYN-RECENT": "too_recent",
        "SYN-STATUS": "status_not_exact",
        "SYN-NOSORT": "unmapped_carrier",
    }
    assert preview.summary.eligible_rows == 1
