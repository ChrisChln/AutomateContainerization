from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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


def test_detects_bag_states_and_extracts_bound_values() -> None:
    empty = observe("single_empty.xml")
    ready = observe("bag_ready.xml")

    assert empty.state is PageState.SINGLE_EMPTY
    assert empty.visible_labels["大包号"] == ""
    assert len(empty.focused_inputs) == 1
    assert empty.focused_inputs[0].bounds == (20, 180, 400, 240)
    assert ready.state is PageState.BAG_READY
    assert ready.visible_labels["大包号"] == "SYN_CONTAINER_01"
    assert ready.visible_labels["排序码"] == "SYN_SORT_01"
    assert ready.latest_tracking_records == ("SYN_TRACK_OLD",)
    assert ready.visible_labels["仓库"] == "SYN-WAREHOUSE"


def test_detects_login_home_and_container_menu() -> None:
    assert observe("login.xml").state is PageState.LOGIN
    assert observe("home.xml").state is PageState.HOME
    assert observe("container_menu.xml").state is PageState.CONTAINER_MENU


def test_duplicate_dialog_requires_exact_verified_error_and_logid() -> None:
    duplicate = observe("duplicate_dialog.xml")

    assert duplicate.state is PageState.DUPLICATE_DIALOG
    assert duplicate.logid == "SYN-LOG-01"
    assert duplicate.duplicate_in_bag
    assert duplicate.dialog_tracking == "SYN_TRACK_NEW"
    assert duplicate.dialog_text.startswith("已扫描包裹已在当前大包中。; Logid=SYN-LOG-01")
    assert len(duplicate.dialog_inputs) == 1
    assert duplicate.dialog_inputs[0].resource_id == "retry_tracking"


def test_error_text_is_not_a_recent_scan_record() -> None:
    duplicate = observe("duplicate_dialog.xml")

    assert duplicate.latest_tracking_records == ()


def test_tracking_in_an_error_node_is_not_a_latest_record() -> None:
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8")
    xml = xml.replace(
        "</hierarchy>",
        '<node package="com.ss.android.pda" class="android.widget.TextView" '
        'text="SYN_TRACK_NEW 系统错误" visible-to-user="true" enabled="true" '
        'bounds="[0,400][300,440]" resource-id="error_text" /></hierarchy>',
    )
    observation = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert observation.latest_tracking_records == ("SYN_TRACK_OLD",)


def test_unknown_and_wrong_package_nodes_fail_closed() -> None:
    unknown = observe("unknown.xml")
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8").replace(
        "com.ss.android.pda", "com.example.other"
    )
    wrong_package = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert unknown.state is PageState.UNKNOWN
    assert wrong_package.state is PageState.UNKNOWN


def test_snapshot_package_must_match_before_classification() -> None:
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8")
    observation = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="")
    )

    assert observation.state is PageState.UNKNOWN


@pytest.mark.parametrize("node_package", [None, "com.example.other"])
def test_visible_relevant_nodes_must_identify_the_target_package(
    node_package: str | None,
) -> None:
    package_attr = f'package="{node_package}" ' if node_package else ""
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8").replace(
        "</hierarchy>",
        f'<node {package_attr}class="android.widget.TextView" text="EXTERNAL_STATUS" '
        'visible-to-user="true" enabled="true" bounds="[0,410][200,450]" '
        'resource-id="external_status" /></hierarchy>',
    )
    observation = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert observation.state is PageState.UNKNOWN


def test_nodes_without_proven_visibility_are_not_used() -> None:
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8").replace(
        ' visible-to-user="true"', ""
    )
    observation = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert observation.state is PageState.UNKNOWN


def test_multiple_enabled_scan_fields_are_not_treated_as_safe() -> None:
    xml = (FIXTURES / "bag_ready.xml").read_text(encoding="utf-8").replace(
        "</hierarchy>",
        '<node package="com.ss.android.pda" class="android.widget.EditText" '
        'text="" focused="false" visible-to-user="true" enabled="true" '
        'bounds="[20,250][400,300]" resource-id="second_input" /></hierarchy>',
    )
    observation = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert observation.state is PageState.UNKNOWN


def test_login_word_inside_other_text_is_not_a_login_page() -> None:
    xml = (FIXTURES / "home.xml").read_text(encoding="utf-8").replace(
        "</hierarchy>",
        '  <node package="com.ss.android.pda" class="android.widget.TextView" '
        'text="登录人：synthetic-operator" visible-to-user="true" enabled="true" '
        'bounds="[0,170][300,210]" resource-id="operator_text" />\n</hierarchy>',
    )
    snapshot = XmlSnapshot(
        xml=xml, captured_at_utc=datetime.now(UTC), package_name="com.ss.android.pda"
    )

    observation = StateDetector(target_package="com.ss.android.pda").detect(snapshot)

    assert observation.state is PageState.HOME


def test_real_containerization_page_is_container_menu_with_one_single_sort_target() -> None:
    xml = (FIXTURES / "real_containerization.xml").read_text(encoding="utf-8")
    snapshot = XmlSnapshot(
        xml=xml, captured_at_utc=datetime.now(UTC), package_name="com.ss.android.pda"
    )
    detector = StateDetector(target_package="com.ss.android.pda")

    observation = detector.detect(snapshot)
    target = detector.find_unique_target(
        snapshot, expected_state=PageState.CONTAINER_MENU, exact_texts=("单排序",)
    )

    assert observation.state is PageState.CONTAINER_MENU
    assert target.text == "单排序"
    assert target.bounds == (45, 120, 382, 154)


def test_real_wms_home_reads_warehouse_and_one_group_bag_target() -> None:
    xml = (FIXTURES / "real_wms_home.xml").read_text(encoding="utf-8")
    snapshot = XmlSnapshot(
        xml=xml, captured_at_utc=datetime.now(UTC), package_name="com.ss.android.pda"
    )
    detector = StateDetector(target_package="com.ss.android.pda")

    observation = detector.detect(snapshot)
    target = detector.find_unique_target(
        snapshot, expected_state=PageState.HOME, exact_texts=("组大包",)
    )

    assert observation.state is PageState.HOME
    assert observation.visible_labels["仓库"] == "WAREHOUSE_TEST"
    assert target.text == "组大包"


# --- 真机“单排序”页面（脱敏 XML） ---------------------------------------------------


def test_real_single_sort_empty_page() -> None:
    page = observe("real_single_sort_empty.xml")

    assert page.state is PageState.SINGLE_EMPTY
    assert page.visible_labels.get("大包号", "") == ""
    assert page.visible_labels.get("排序码", "") == ""
    assert len(page.input_fields) == 1
    assert page.input_fields[0].text == ""
    assert page.focused_inputs[0].bounds == (34, 639, 445, 712)


def test_real_bag_ready_reads_label_adjacent_values_without_focus() -> None:
    page = observe("real_single_sort_bag_ready.xml")

    assert page.state is PageState.BAG_READY
    assert page.visible_labels["大包号"] == "CT0000000000001"
    assert page.visible_labels["排序码"] == "CARRIER_TEST_01"
    assert page.visible_labels["总包裹数量"] == "17"
    assert page.latest_tracking_records == ()
    assert page.focused_inputs == ()
    assert len(page.input_fields) == 1
    assert page.input_fields[0].bounds == (34, 639, 445, 712)
    assert not page.input_fields[0].focused


def test_real_success_page_reads_latest_records_under_last_scanned_anchor() -> None:
    page = observe("real_single_sort_success.xml")

    assert page.state is PageState.SCAN_RESULT
    assert page.latest_tracking_records == (
        "TRACKING_TEST_001",
        "TRACKING_TEST_002",
        "TRACKING_TEST_003",
    )
    assert page.visible_labels["排序码"] == "CARRIER_TEST_01"
    assert "大包号" not in page.visible_labels
    assert page.visible_labels["扫描数量"] == "18"
    assert page.dialog_text == ""
    assert page.logid is None


def test_real_scrolled_records_without_anchor_are_not_latest_records() -> None:
    page = observe("real_single_sort_success_scrolled.xml")

    assert page.latest_tracking_records == ()
    assert page.state is PageState.BAG_READY
    assert page.visible_labels["大包号"] == "CT0000000000001"


def test_real_duplicate_dialog_is_recognized_from_dialog_subtree() -> None:
    page = observe("real_single_sort_duplicate.xml")

    assert page.state is PageState.DUPLICATE_DIALOG
    assert page.duplicate_in_bag
    assert page.logid == "TEST_LOG_ID"
    assert page.dialog_tracking == "TRACKING_TEST_001"
    assert len(page.input_fields) == 2
    assert len(page.dialog_inputs) == 1
    assert page.dialog_inputs[0].text == ""
    assert page.dialog_inputs[0].bounds == (42, 597, 438, 670)


def _real_duplicate_with_body(body: str):
    xml = (FIXTURES / "real_single_sort_duplicate.xml").read_text(encoding="utf-8")
    original = (
        "已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID,"
        "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001"
    )
    assert original in xml
    return StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(
            xml.replace(original, body), datetime.now(UTC), package_name="com.ss.android.pda"
        )
    )


def test_real_duplicate_accepts_body_without_trailing_period() -> None:
    page = _real_duplicate_with_body(
        "已扫描包裹已在当前大包中; Logid=TEST_LOG_ID,"
        "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001"
    )

    assert page.state is PageState.DUPLICATE_DIALOG
    assert page.dialog_tracking == "TRACKING_TEST_001"


@pytest.mark.parametrize(
    "body",
    [
        # 模糊包含：前后多了文字
        "注意：已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID,"
        "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001",
        "已扫描包裹已在其他大包中。; Logid=TEST_LOG_ID,"
        "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001",
        # Logid 为空
        "已扫描包裹已在当前大包中。; Logid=,"
        "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001",
        # 没有运单号
        "已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID,请重新扫描追踪号码，然后扫描正确的容器",
    ],
)
def test_real_duplicate_rejects_non_exact_body(body: str) -> None:
    page = _real_duplicate_with_body(body)

    assert page.state is not PageState.DUPLICATE_DIALOG
    assert not page.duplicate_in_bag


def test_conflicting_logids_on_one_page_are_not_trusted() -> None:
    xml = (FIXTURES / "real_single_sort_duplicate.xml").read_text(encoding="utf-8")
    xml = xml.replace(
        "已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID\"",
        "已扫描包裹已在当前大包中。; Logid=OTHER_LOG_ID\"",
    )
    assert "OTHER_LOG_ID" in xml
    page = StateDetector(target_package="com.ss.android.pda").detect(
        XmlSnapshot(xml, datetime.now(UTC), package_name="com.ss.android.pda")
    )

    assert page.logid is None
    assert page.state is not PageState.DUPLICATE_DIALOG
