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
    assert duplicate.dialog_text == "已扫描包裹已在当前大包中"
    assert len(duplicate.focused_inputs) == 1
    assert duplicate.focused_inputs[0].resource_id == "retry_tracking"


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
