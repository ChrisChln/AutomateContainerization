from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    ScanOutcome,
)
from tt_auto_bag_scanner.infrastructure.exception_handler import UnknownPageError
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.infrastructure.wms_controller import (
    MixedSortController,
    ScanNotSent,
    SubmissionBlocked,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wms"
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
WAREHOUSE = "WAREHOUSE_TEST"
TRACKING = "TESTTRK000000000001"
SORT = "SORT_TEST_01"
BAG = "SYN_BAG_MIXED_01"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def focused(xml: str) -> str:
    """The same page after the operator-visible input gained focus."""
    head, _, tail = xml.partition('class="android.widget.EditText"')
    tail = tail.replace('focused="false"', 'focused="true"', 1)
    return head + 'class="android.widget.EditText"' + tail


EMPTY = fixture("real_mixed_sort_empty.xml")
AWAIT = fixture("real_mixed_sort_await_bag.xml")
SUCCESS = fixture("real_mixed_sort_success.xml")
GROUPING = fixture("real_mixed_sort_grouping_dialog.xml")
HOME = fixture("real_wms_home.xml")
MENU = fixture("real_containerization.xml")


def config(sort_code: str = SORT) -> CarrierConfig:
    return CarrierConfig(
        carrier_key="SYN_CARRIER",
        display_name="Synthetic",
        aliases=(),
        sort_code=sort_code,
        container_no=BAG,
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )


def item(sort_code: str = SORT) -> QueueItem:
    return QueueItem(
        id="item-mixed-1",
        task_id="task-mixed-1",
        tracking=TRACKING,
        carrier_key="SYN_CARRIER",
        sort_code=sort_code,
        container_no=BAG,
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
        status=ItemStatus.READY,
    )


def check(queue_item: QueueItem) -> PhysicalCheck:
    return PhysicalCheck(
        id="check-mixed-1",
        item_id=queue_item.id,
        tracking=queue_item.tracking,
        container_no=queue_item.container_no,
        operator="synthetic-operator",
        checked_at_utc=NOW,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="synthetic-evidence",
    )


def controller_on_mixed_page(*, after_tap=(), after_enter=(), after_back=()):
    """Start on HOME (warehouse verified), navigate to 混合排序, then the scripted pages."""
    adapter = FakeDeviceAdapter(
        initial_xml=HOME,
        snapshots_after_tap=(MENU, EMPTY, *after_tap),
        snapshots_after_enter=after_enter,
        snapshots_after_back=after_back,
    )
    controller = MixedSortController(adapter, warehouse=WAREHOUSE, poll_attempts=2)
    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)
    return adapter, controller, session


def entered(adapter: FakeDeviceAdapter) -> list[str]:
    return [action[1] for action in adapter.actions if action[0] == "enter_text"]


def keys(adapter: FakeDeviceAdapter) -> list[str]:
    return [action[1] for action in adapter.actions if action[0] == "press_key"]


def test_navigates_home_menu_to_mixed_sort_and_issues_item_session() -> None:
    adapter, controller, session = controller_on_mixed_page()

    assert session.container_no == BAG
    assert session.sort_code == SORT
    assert session.warehouse == WAREHOUSE
    assert controller.active_session == session
    assert entered(adapter) == []
    assert keys(adapter) == []


def test_tracking_then_bag_is_success_when_count_increments() -> None:
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(AWAIT)),
        after_enter=(AWAIT, SUCCESS),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.SUCCESS
    assert result.evidence.latest_tracking == TRACKING
    assert result.evidence.sort_code == SORT
    assert result.evidence.container_no == BAG
    assert entered(adapter) == [TRACKING, BAG]
    assert keys(adapter) == ["ENTER", "ENTER"]


def test_wrong_sort_code_never_types_the_bag() -> None:
    other = config("SORT_OTHER_99")
    adapter2 = FakeDeviceAdapter(
        initial_xml=HOME,
        snapshots_after_tap=(MENU, EMPTY, focused(EMPTY)),
        snapshots_after_enter=(AWAIT,),
    )
    controller = MixedSortController(adapter2, warehouse=WAREHOUSE, poll_attempts=2)
    session = controller.verify_current_or_open_bag(other, unresolved_submissions=0)
    queue_item = item("SORT_OTHER_99")

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.REJECTED
    assert not result.skip_rejected_item
    assert "SORT_TEST_01" in result.evidence.error_text
    assert entered(adapter2) == [TRACKING]
    assert keys(adapter2) == ["ENTER"]


@pytest.mark.parametrize("after_bag", [False, True])
@pytest.mark.parametrize("matching_tracking", [False, True])
def test_already_grouped_dialog_is_a_duplicate_only_for_current_tracking(
    after_bag, matching_tracking
) -> None:
    duplicate = GROUPING.replace("状态异常，状态为组包中", "状态异常, 状态为已组包")
    duplicate = duplicate.replace(TRACKING, TRACKING if matching_tracking else "OTHER_TRACKING")
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(AWAIT)) if after_bag else (focused(EMPTY),),
        after_enter=(AWAIT, duplicate) if after_bag else (duplicate,),
        after_back=(EMPTY,),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert entered(adapter) == ([TRACKING, BAG] if after_bag else [TRACKING])
    if matching_tracking:
        assert result.outcome is ScanOutcome.REJECTED
        assert result.skip_rejected_item
        assert result.evidence.error_text == "重复扫描，已跳过"
        assert result.evidence.logid == "LOGIDTEST0000000000000000000001"
        assert keys(adapter) == (["ENTER", "ENTER", "BACK"] if after_bag else ["ENTER", "BACK"])
    else:
        assert result.outcome is ScanOutcome.UNKNOWN
        assert "BACK" not in keys(adapter)


def test_grouping_dialog_after_tracking_closes_once_and_skips_item() -> None:
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY),), after_enter=(GROUPING,)
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert result.evidence.logid == "LOGIDTEST0000000000000000000001"
    assert entered(adapter) == [TRACKING]
    # The scripted dialog never closes: one extra Back, then the session is dropped.
    assert keys(adapter) == ["ENTER", "BACK", "BACK"]
    assert controller.active_session is None


def test_unchanged_count_after_bag_is_unknown_and_drops_session() -> None:
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(AWAIT)),
        after_enter=(AWAIT, EMPTY),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.UNKNOWN
    assert controller.active_session is None
    assert keys(adapter) == ["ENTER", "ENTER"]


def test_unreadable_page_after_tracking_is_unknown_without_typing_bag() -> None:
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY),), after_enter=(fixture("unknown.xml"),)
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.UNKNOWN
    assert entered(adapter) == [TRACKING]


def test_input_that_does_not_take_focus_is_not_sent() -> None:
    adapter, controller, session = controller_on_mixed_page(after_tap=(EMPTY,))
    queue_item = item()

    with pytest.raises(ScanNotSent):
        controller.submit_once(queue_item, session, check(queue_item), "attempt-1")
    assert entered(adapter) == []
    assert keys(adapter) == []


def test_submission_requires_the_mixed_empty_page() -> None:
    adapter, controller, session = controller_on_mixed_page(after_tap=(focused(EMPTY),))
    adapter._current_xml = AWAIT
    queue_item = item()

    with pytest.raises((UnknownPageError, SubmissionBlocked)):
        controller.submit_once(queue_item, session, check(queue_item), "attempt-1")
    assert entered(adapter) == []


def test_switching_carrier_reverifies_page_without_back_or_close() -> None:
    adapter, controller, _ = controller_on_mixed_page()
    other = CarrierConfig(
        carrier_key="SYN_OTHER",
        display_name="Other",
        aliases=(),
        sort_code="SORT_OTHER_02",
        container_no="SYN_BAG_MIXED_02",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    session = controller.switch_bag(other, unresolved_submissions=0)

    assert session.container_no == "SYN_BAG_MIXED_02"
    assert keys(adapter) == []
    assert not any(action[0] == "tap" and "关闭" in str(action) for action in adapter.actions)


def test_mixed_page_with_unverified_warehouse_backs_out_to_home_first() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=EMPTY,
        snapshots_after_back=(MENU, HOME),
        snapshots_after_tap=(MENU, EMPTY),
    )
    controller = MixedSortController(adapter, warehouse=WAREHOUSE, poll_attempts=2)

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert session.container_no == BAG
    assert keys(adapter) == ["BACK", "BACK"]


def test_recover_duplicate_is_never_used_in_mixed_mode() -> None:
    _, controller, session = controller_on_mixed_page()

    with pytest.raises(SubmissionBlocked):
        controller.recover_duplicate(object(), session)  # type: ignore[arg-type]


def test_leftover_inline_grouping_text_is_cleared_without_submitting() -> None:
    inline = fixture("real_mixed_sort_grouping_inline.xml")
    cleared = inline.replace('text="TESTTRK000000000002"', 'text=""')
    adapter, controller, _ = controller_on_mixed_page(after_tap=(focused(inline),))
    controller._drop_session()  # a skipped 组包中 item drops the session
    adapter._current_xml = inline
    adapter._next_clear_snapshots = [cleared]

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert session.container_no == BAG
    assert entered(adapter) == []
    assert keys(adapter) == []
    assert ("clear_text",) in adapter.actions


def test_leftover_inline_text_is_kept_while_submissions_are_unresolved() -> None:
    inline = fixture("real_mixed_sort_grouping_inline.xml")
    adapter = FakeDeviceAdapter(initial_xml=inline)
    controller = MixedSortController(adapter, warehouse=WAREHOUSE, poll_attempts=1)

    with pytest.raises(UnknownPageError):
        controller.verify_current_or_open_bag(config(), unresolved_submissions=1)
    assert ("clear_text",) not in adapter.actions


def test_fresh_controller_clears_leftover_then_verifies_warehouse_on_home() -> None:
    inline = fixture("real_mixed_sort_grouping_inline.xml")
    cleared = inline.replace('text="TESTTRK000000000002"', 'text=""')
    adapter = FakeDeviceAdapter(
        initial_xml=inline,
        snapshots_after_tap=(focused(inline), MENU, cleared),
        snapshots_after_clear=(cleared,),
        snapshots_after_back=(MENU, HOME),
    )
    controller = MixedSortController(adapter, warehouse=WAREHOUSE, poll_attempts=2)

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert session.container_no == BAG
    assert entered(adapter) == []
    assert keys(adapter) == ["BACK", "BACK"]
    assert ("clear_text",) in adapter.actions


def test_grouping_skip_then_next_item_clears_leftover_without_going_home() -> None:
    inline = fixture("real_mixed_sort_grouping_inline.xml")
    cleared = inline.replace('text="TESTTRK000000000002"', 'text=""')
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(inline)),
        after_enter=(fixture("unknown.xml"),),
        after_back=(inline,),
    )
    adapter._next_clear_snapshots = [cleared]
    real_observe = controller.observe
    frames = iter((fixture("unknown.xml"), GROUPING))

    def observe_rendering_dialog(**kwargs):
        if kwargs.get("reading_scan_result") and adapter._current_xml.startswith(
            fixture("unknown.xml")[:40]
        ):
            adapter._current_xml = next(frames, GROUPING)
        return real_observe(**kwargs)

    controller.observe = observe_rendering_dialog  # type: ignore[method-assign]
    queue_item = item()
    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")
    assert result.skip_rejected_item

    controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert keys(adapter) == ["ENTER", "BACK"]
    assert ("clear_text",) in adapter.actions


def test_keyboard_layout_after_tracking_still_types_bag_into_focused_input() -> None:
    keyboard = fixture("real_mixed_sort_await_bag_keyboard.xml")
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY),),
        after_enter=(keyboard, SUCCESS),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.SUCCESS
    assert entered(adapter) == [TRACKING, BAG]
    assert keys(adapter) == ["ENTER", "ENTER"]


def test_leftover_await_bag_page_is_left_by_back_without_typing_a_bag() -> None:
    keyboard = fixture("real_mixed_sort_await_bag_keyboard.xml")
    adapter, controller, _ = controller_on_mixed_page(after_tap=(EMPTY,))
    controller._drop_session()  # the previous item ended UNKNOWN
    adapter._current_xml = keyboard
    adapter._next_back_snapshots = [AWAIT, MENU]

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert session.container_no == BAG
    assert entered(adapter) == []
    assert keys(adapter) == ["BACK", "BACK"]


def test_await_bag_page_is_not_left_while_submissions_are_unresolved() -> None:
    adapter, controller, _ = controller_on_mixed_page()
    controller._drop_session()
    adapter._current_xml = AWAIT

    with pytest.raises(UnknownPageError):
        controller.verify_current_or_open_bag(config(), unresolved_submissions=1)
    assert keys(adapter) == []


def test_transient_unreadable_frame_after_tracking_still_types_bag() -> None:
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(AWAIT)),
        after_enter=(fixture("unknown.xml"), SUCCESS),
    )
    real_snapshot = adapter.snapshot_xml
    shown = {"unknown": 0}

    def snapshot():
        if adapter._current_xml == fixture("unknown.xml"):
            shown["unknown"] += 1
            if shown["unknown"] > 1:
                adapter._current_xml = AWAIT
        return real_snapshot()

    adapter.snapshot_xml = snapshot  # type: ignore[method-assign]
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.SUCCESS
    assert entered(adapter) == [TRACKING, BAG]


@pytest.mark.parametrize("message", ["状态异常，状态为组包中", "状态异常, 状态为已组包"])
def test_grouping_skip_clears_leftover_and_keeps_session_for_next_item(message: str) -> None:
    inline = fixture("real_mixed_sort_grouping_inline.xml").replace(
        "TESTTRK000000000002", TRACKING
    )
    inline = inline.replace("状态异常，状态为组包中", message)
    cleared = inline.replace(f'text="{TRACKING}"', 'text=""')
    next_tracking = "TESTTRK000000000003"
    next_await = AWAIT.replace(TRACKING, next_tracking)
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(inline), focused(cleared), focused(next_await)),
        after_enter=(GROUPING.replace("状态异常，状态为组包中", message), next_await, SUCCESS),
        after_back=(inline,),
    )
    adapter._next_clear_snapshots = [cleared]
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.skip_rejected_item
    assert keys(adapter) == ["ENTER", "BACK"]
    assert ("clear_text",) in adapter.actions
    assert controller.active_session == session
    assert controller.verify_current_or_open_bag(config(), unresolved_submissions=0) == session
    assert keys(adapter) == ["ENTER", "BACK"]
    next_item = replace(queue_item, id="item-mixed-2", tracking=next_tracking)
    next_result = controller.submit_once(next_item, session, check(next_item), "attempt-2")
    assert next_result.outcome is ScanOutcome.SUCCESS
    assert entered(adapter) == [TRACKING, next_tracking, BAG]


def test_tracking_input_can_move_up_when_the_keyboard_opens() -> None:
    keyboard = focused(EMPTY).replace("[34,639][445,712]", "[34,410][445,483]")
    keyboard = keyboard.replace(
        "</hierarchy>",
        '<node package="com.android.inputmethod.latin" visible-to-user="true" />'
        "</hierarchy>",
    )
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(keyboard, focused(AWAIT)), after_enter=(AWAIT, SUCCESS),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.SUCCESS
    assert entered(adapter) == [TRACKING, BAG]


def test_unreadable_page_after_unknown_item_is_left_by_one_back() -> None:
    adapter, controller, _ = controller_on_mixed_page(after_tap=(EMPTY,))
    controller._drop_session()  # the previous item ended UNKNOWN
    adapter._current_xml = fixture("unknown.xml")
    adapter._next_back_snapshots = [EMPTY, MENU, HOME]
    adapter._next_tap_snapshots = [MENU, EMPTY]

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    # One Back leaves the unreadable page; the warehouse is re-verified on HOME.
    assert session.container_no == BAG
    assert entered(adapter) == []
    assert keys(adapter) == ["BACK", "BACK", "BACK"]


def test_unreadable_page_is_not_left_while_submissions_are_unresolved() -> None:
    adapter, controller, _ = controller_on_mixed_page()
    controller._drop_session()
    adapter._current_xml = fixture("unknown.xml")

    with pytest.raises(UnknownPageError):
        controller.verify_current_or_open_bag(config(), unresolved_submissions=1)
    assert keys(adapter) == []


def test_failed_scan_text_left_in_input_is_cleared_without_back() -> None:
    adapter, controller, _ = controller_on_mixed_page()
    controller._drop_session()  # the previous item ended UNKNOWN
    adapter._current_xml = fixture("real_mixed_sort_scan_failed_inline.xml")
    adapter._next_clear_snapshots = [EMPTY]

    session = controller.verify_current_or_open_bag(config(), unresolved_submissions=0)

    assert session.container_no == BAG
    assert entered(adapter) == []
    assert keys(adapter) == []
    assert ("clear_text",) in adapter.actions


@pytest.mark.parametrize(
    ("fixture_name", "error"),
    [
        ("real_mixed_sort_grouping_inline.xml", "状态异常，状态为组包中"),
        ("real_mixed_sort_scan_failed_inline.xml", "扫描失败"),
    ],
)
def test_inline_error_after_tracking_is_cleared_and_skipped_without_back(
    fixture_name: str, error: str
) -> None:
    inline = fixture(fixture_name).replace("TESTTRK000000000002", TRACKING)
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(inline)),
        after_enter=(inline,),
    )
    adapter._next_clear_snapshots = [EMPTY]
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert error in (result.evidence.error_text or "")
    assert entered(adapter) == [TRACKING]
    assert keys(adapter) == ["ENTER"]
    assert ("clear_text",) in adapter.actions
    assert controller.active_session == session
    assert controller.verify_current_or_open_bag(config(), unresolved_submissions=0) == session
    assert keys(adapter) == ["ENTER"]


def closed_container_dialog(xml: str) -> str:
    """Synthetic 重新打开这个容器？ dialog laid over a 混合排序 page."""
    dialog = (
        '<node index="9" text="" resource-id="" class="android.view.View" '
        'package="com.ss.android.pda" enabled="true" visible-to-user="true" '
        'bounds="[0,180][480,480]">'
        '<node index="0" text="重新打开这个容器?" resource-id="" '
        'class="android.widget.TextView" package="com.ss.android.pda" enabled="true" '
        'visible-to-user="true" bounds="[40,200][400,250]" />'
        '<node index="1" text="此容器已关闭。您确定要重新打开它吗?" resource-id="" '
        'class="android.widget.TextView" package="com.ss.android.pda" enabled="true" '
        'visible-to-user="true" bounds="[40,260][460,330]" />'
        '<node index="2" text="取消" resource-id="" class="android.widget.Button" '
        'package="com.ss.android.pda" clickable="true" enabled="true" '
        'visible-to-user="true" bounds="[40,380][240,450]" />'
        '<node index="3" text="确认" resource-id="" class="android.widget.Button" '
        'package="com.ss.android.pda" clickable="true" enabled="true" '
        'visible-to-user="true" bounds="[260,380][460,450]" />'
        "</node>"
    )
    head, _, tail = xml.rpartition("</node></hierarchy>")
    return head + dialog + "</node></hierarchy>" + tail


def test_closed_container_dialog_taps_cancel_and_blocks_for_new_bag() -> None:
    dialog = closed_container_dialog(AWAIT)
    adapter, controller, session = controller_on_mixed_page(
        after_tap=(focused(EMPTY), focused(AWAIT), AWAIT),
        after_enter=(AWAIT, dialog),
    )
    queue_item = item()

    result = controller.submit_once(queue_item, session, check(queue_item), "attempt-1")

    assert result.outcome is ScanOutcome.REJECTED
    assert not result.skip_rejected_item
    assert "大包号" in result.evidence.error_text
    assert entered(adapter) == [TRACKING, BAG]
    assert keys(adapter) == ["ENTER", "ENTER"]
    taps = [action for action in adapter.actions if action[0] == "tap"]
    assert taps[-1][1:] == (140, 415)
    assert controller.active_session is None
