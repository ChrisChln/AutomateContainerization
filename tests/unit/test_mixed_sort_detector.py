from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tt_auto_bag_scanner.domain.models import PageState, XmlSnapshot
from tt_auto_bag_scanner.infrastructure.state_detector import StateDetector

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wms"


def observe(name: str):
    snapshot = XmlSnapshot(
        xml=(FIXTURES / name).read_text(encoding="utf-8"),
        captured_at_utc=datetime.now(UTC),
        package_name="com.ss.android.pda",
    )
    return StateDetector(target_package="com.ss.android.pda").detect(snapshot)


def test_real_mixed_empty_page_reports_count_and_one_empty_input() -> None:
    page = observe("real_mixed_sort_empty.xml")

    assert page.state is PageState.MIXED_EMPTY
    assert page.mixed_scanned_count == 0
    assert page.mixed_tracking == ""
    assert len(page.input_fields) == 1
    assert page.input_fields[0].text == ""
    assert "排序码" not in page.visible_labels


def test_real_mixed_page_after_tracking_shows_sort_code_above_its_label() -> None:
    page = observe("real_mixed_sort_await_bag.xml")

    assert page.state is PageState.MIXED_AWAIT_BAG
    assert page.visible_labels["排序码"] == "SORT_TEST_01"
    assert page.mixed_tracking == "TESTTRK000000000001"
    assert page.mixed_scanned_count is None
    assert len(page.input_fields) == 1


def test_real_mixed_success_returns_to_empty_with_incremented_count() -> None:
    page = observe("real_mixed_sort_success.xml")

    assert page.state is PageState.MIXED_EMPTY
    assert page.mixed_scanned_count == 1
    assert page.input_fields[0].focused


def test_real_mixed_grouping_dialog_names_the_scanned_tracking() -> None:
    page = observe("real_mixed_sort_grouping_dialog.xml")

    assert page.state is PageState.UNKNOWN
    assert page.mixed_grouping_tracking == "TESTTRK000000000001"
    assert page.logid == "LOGIDTEST0000000000000000000001"


def test_container_menu_still_detected_and_single_sort_unchanged() -> None:
    assert observe("container_menu.xml").state is PageState.CONTAINER_MENU
    assert observe("real_single_sort_empty.xml").state is PageState.SINGLE_EMPTY
    assert observe("real_single_sort_empty.xml").mixed_scanned_count is None


def test_real_mixed_inline_grouping_error_leaves_tracking_in_input() -> None:
    page = observe("real_mixed_sort_grouping_inline.xml")

    assert page.state is PageState.UNKNOWN
    assert page.mixed_inline_grouping_tracking == "TESTTRK000000000002"
    assert page.mixed_scanned_count == 0


def test_real_mixed_await_bag_with_keyboard_hides_tracking_but_keeps_sort() -> None:
    page = observe("real_mixed_sort_await_bag_keyboard.xml")

    assert page.state is PageState.MIXED_AWAIT_BAG
    assert page.keyboard_visible
    assert page.visible_labels["排序码"] == "SORT_TEST_01"
    assert page.mixed_tracking == ""
    assert page.input_fields[0].focused


def test_mixed_scan_failed_message_leaves_tracking_in_input() -> None:
    page = observe("real_mixed_sort_scan_failed_inline.xml")

    assert page.state is PageState.UNKNOWN
    assert page.mixed_failed_input_text == "TESTTRK000000000002"
    assert page.mixed_inline_grouping_tracking == ""
