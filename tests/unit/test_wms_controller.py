from __future__ import annotations

import re as _re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceMode,
    ItemStatus,
    PhysicalCheck,
    PhysicalCheckMethod,
    QueueItem,
    ScanOutcome,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.adb_adapter import AdbDeviceAdapter
from tt_auto_bag_scanner.infrastructure.exception_handler import UnknownPageError
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.infrastructure.wms_controller import (
    SubmissionBlocked,
    WmsController,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wms"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_controller_mode_reflects_simulated_or_real_adapter() -> None:
    simulated = WmsController(FakeDeviceAdapter(), warehouse="SYN-WAREHOUSE")
    real = WmsController(AdbDeviceAdapter(), warehouse="SYN-WAREHOUSE")

    assert simulated.device_mode is DeviceMode.SIMULATED
    assert real.device_mode is DeviceMode.REAL


def make_config() -> CarrierConfig:
    return CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER",
        display_name="Synthetic carrier",
        aliases=(),
        sort_code="SYN_SORT_01",
        container_no="SYN_CONTAINER_01",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )


def make_item(status: ItemStatus = ItemStatus.READY) -> QueueItem:
    return QueueItem(
        id="item-syn-1",
        task_id="task-syn-1",
        tracking="SYN_TRACK_NEW",
        carrier_key="SYNTHETIC_CARRIER",
        sort_code="SYN_SORT_01",
        container_no="SYN_CONTAINER_01",
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
        status=status,
    )


def make_check(item: QueueItem, *, container_no: str | None = None) -> PhysicalCheck:
    return PhysicalCheck(
        id="check-syn-1",
        item_id=item.id,
        tracking=item.tracking,
        container_no=container_no or item.container_no,
        operator="synthetic-operator",
        checked_at_utc=NOW,
        method=PhysicalCheckMethod.MANUAL_CONFIRMATION,
        evidence_ref="synthetic-evidence",
    )


def make_controller(*snapshots_after_enter: str):
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        devices=(),
        snapshots_after_enter=snapshots_after_enter,
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    session = controller.open_bag(make_config())
    return adapter, controller, session


def test_open_bag_verifies_actual_container_sort_and_session() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        devices=(),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    session = controller.open_bag(make_config())

    assert session.container_no == "SYN_CONTAINER_01"
    assert session.sort_code == "SYN_SORT_01"
    assert session.valid
    assert [action[0] for action in adapter.actions].count("press_key") == 1
    assert adapter.adb_write_call_count == 0


def test_open_bag_navigates_home_to_container_menu_to_single_sort() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("home.xml"),
        snapshots_after_tap=(
            read_fixture("container_menu.xml"),
            read_fixture("single_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    session = controller.open_bag(make_config())

    taps = [action for action in adapter.actions if action[0] == "tap"]
    assert taps[:2] == [("tap", 110, 130), ("tap", 100, 125)]
    assert session.container_no == "SYN_CONTAINER_01"
    assert adapter.entered_text == ("SYN_CONTAINER_01",)


def test_open_bag_accepts_exact_management_label_from_home() -> None:
    home = read_fixture("home.xml").replace("组大包", "大包管理")
    adapter = FakeDeviceAdapter(
        initial_xml=home,
        snapshots_after_tap=(
            read_fixture("container_menu.xml"),
            read_fixture("single_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    controller.open_bag(make_config())

    taps = [action for action in adapter.actions if action[0] == "tap"]
    assert taps[:2] == [("tap", 110, 130), ("tap", 100, 125)]


def test_navigation_fails_without_one_visible_exact_home_target() -> None:
    duplicate_target = read_fixture("home.xml").replace(
        "</hierarchy>",
        '<node package="com.ss.android.pda" class="android.widget.Button" '
        'text="组大包" visible-to-user="true" enabled="true" '
        'bounds="[220,100][420,160]" resource-id="second_group_bag_button" /></hierarchy>',
    )
    adapter = FakeDeviceAdapter(initial_xml=duplicate_target)
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    with pytest.raises(UnknownPageError, match="target is missing or ambiguous"):
        controller.open_bag(make_config())
    assert not any(action[0] in {"tap", "enter_text", "press_key"} for action in adapter.actions)


def test_container_menu_without_single_sort_never_taps_mixed_sort() -> None:
    menu = read_fixture("container_menu.xml").replace(
        'text="单排序" visible-to-user="true"',
        'text="单排序" visible-to-user="false"',
    )
    adapter = FakeDeviceAdapter(initial_xml=menu)
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    with pytest.raises(UnknownPageError, match="target is missing or ambiguous"):
        controller.open_bag(make_config())
    assert not any(action[0] in {"tap", "enter_text", "press_key"} for action in adapter.actions)


def test_open_bag_fails_closed_on_unknown_page() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("unknown.xml"))
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    with pytest.raises(UnknownPageError):
        controller.open_bag(make_config())
    assert not any(action[0] in {"tap", "enter_text", "press_key"} for action in adapter.actions)


def test_login_or_wrong_warehouse_never_navigates() -> None:
    login_adapter = FakeDeviceAdapter(initial_xml=read_fixture("login.xml"))
    login_controller = WmsController(login_adapter, warehouse="SYN-WAREHOUSE")
    with pytest.raises(UnknownPageError):
        login_controller.open_bag(make_config())
    assert not any(
        action[0] in {"tap", "enter_text", "press_key"}
        for action in login_adapter.actions
    )

    wrong_warehouse = read_fixture("home.xml").replace("SYN-WAREHOUSE", "OTHER-WAREHOUSE")
    wrong_adapter = FakeDeviceAdapter(initial_xml=wrong_warehouse)
    wrong_controller = WmsController(wrong_adapter, warehouse="SYN-WAREHOUSE")
    with pytest.raises(SubmissionBlocked, match="warehouse"):
        wrong_controller.open_bag(make_config())
    assert not any(
        action[0] in {"tap", "enter_text", "press_key"}
        for action in wrong_adapter.actions
    )


def test_verify_current_or_open_bag_is_read_only_on_matching_open_container() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("bag_ready.xml"))
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    session = controller.verify_current_or_open_bag(make_config())

    assert session.container_no == "SYN_CONTAINER_01"
    assert session.sort_code == "SYN_SORT_01"
    assert not any(action[0] in {"tap", "enter_text", "press_key"} for action in adapter.actions)
    assert adapter.entered_text == ()


def test_verify_current_or_open_bag_navigates_safely_from_home() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("home.xml"),
        snapshots_after_tap=(
            read_fixture("container_menu.xml"),
            read_fixture("single_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    session = controller.verify_current_or_open_bag(make_config())

    assert session.container_no == "SYN_CONTAINER_01"
    assert len([action for action in adapter.actions if action[0] == "tap"]) == 3
    assert adapter.entered_text == ("SYN_CONTAINER_01",)


def test_verify_current_or_open_bag_rejects_other_container_without_actions() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("bag_ready.xml"))
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    different_config = CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER_2",
        display_name="Synthetic carrier 2",
        aliases=(),
        sort_code="SYN_SORT_02",
        container_no="SYN_CONTAINER_02",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    with pytest.raises(SubmissionBlocked, match="container"):
        controller.verify_current_or_open_bag(different_config)
    assert not any(action[0] in {"tap", "enter_text", "press_key"} for action in adapter.actions)


def test_submit_requires_physical_check_and_matching_session() -> None:
    adapter, controller, session = make_controller(read_fixture("bag_ready.xml"))
    item = make_item()

    with pytest.raises(SubmissionBlocked, match="physical"):
        controller.submit_once(item, session, physical_check=None, attempt_id="attempt-syn-1")
    with pytest.raises(SubmissionBlocked, match="container"):
        wrong_container_session = VerifiedSession(
            serial=session.serial,
            warehouse=session.warehouse,
            container_no="SYN_OTHER",
            sort_code=session.sort_code,
            session_id=session.session_id,
            verified_at_utc=session.verified_at_utc,
        )
        controller.submit_once(
            item,
            wrong_container_session,
            physical_check=make_check(item),
            attempt_id="attempt-syn-1",
        )

    assert [action[0] for action in adapter.actions].count("press_key") == 1


def test_new_result_requires_latest_record_and_exact_container_and_sort() -> None:
    adapter, controller, session = make_controller(
        read_fixture("bag_ready.xml"), read_fixture("scan_success.xml")
    )
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-syn-1"
    )

    assert result.outcome is ScanOutcome.SUCCESS
    assert result.attempt_id == "attempt-syn-1"
    assert [action[0] for action in adapter.actions].count("press_key") == 2
    assert result.evidence.latest_tracking == item.tracking


def test_duplicate_is_separate_outcome_and_recovery_does_not_resubmit() -> None:
    adapter, controller, session = make_controller(
        read_fixture("bag_ready.xml"),
        read_fixture("duplicate_dialog.xml"),
        read_fixture("bag_ready.xml"),
    )
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-syn-1"
    )
    adapter._next_back_snapshots = [read_fixture("bag_ready.xml")]
    recovery = controller.recover_duplicate(result, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert result.evidence.logid == "SYN-LOG-01"
    assert recovery.recovered
    assert recovery.container_no == item.container_no
    assert [action[0] for action in adapter.actions].count("press_key") == 3
    with pytest.raises(SubmissionBlocked, match="not issued"):
        controller.recover_duplicate(result, session)


def test_failed_duplicate_recovery_cannot_be_repeated() -> None:
    adapter, controller, session = make_controller(
        read_fixture("bag_ready.xml"),
        read_fixture("duplicate_dialog.xml"),
        read_fixture("unknown.xml"),
    )
    item = make_item()
    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-syn-1"
    )

    adapter._next_back_snapshots = [read_fixture("unknown.xml")]
    recovery = controller.recover_duplicate(result, session)

    assert not recovery.recovered
    assert [action[0] for action in adapter.actions].count("press_key") == 3
    with pytest.raises(SubmissionBlocked, match="already attempted"):
        controller.recover_duplicate(result, session)


def test_ambiguous_result_is_unknown_and_never_retries() -> None:
    adapter, controller, session = make_controller(
        read_fixture("bag_ready.xml"), read_fixture("unknown.xml")
    )
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-syn-1"
    )

    assert result.outcome is ScanOutcome.UNKNOWN
    assert [action[0] for action in adapter.actions].count("press_key") == 2
    with pytest.raises(SubmissionBlocked, match="session token was not verified"):
        controller.submit_once(
            item, session, physical_check=make_check(item), attempt_id="attempt-syn-2"
        )
    assert [action[0] for action in adapter.actions].count("press_key") == 2


def test_rejected_item_cannot_be_submitted() -> None:
    adapter, controller, session = make_controller(read_fixture("bag_ready.xml"))

    with pytest.raises(SubmissionBlocked):
        controller.submit_once(
            make_item(ItemStatus.WAIT_PHYSICAL),
            session,
            physical_check=make_check(make_item()),
            attempt_id="attempt-syn-1",
        )


def test_session_token_must_be_created_by_this_controller() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("bag_ready.xml"), devices=())
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    item = make_item()

    with pytest.raises(SubmissionBlocked, match="verified by this controller"):
        controller.submit_once(
            item,
            VerifiedSession(
                serial="simulator",
                warehouse="SYN-WAREHOUSE",
                container_no=item.container_no,
                sort_code=item.sort_code,
                session_id="forged-session",
                verified_at_utc=NOW,
            ),
            physical_check=make_check(item),
            attempt_id="attempt-syn-1",
        )


def test_real_wms_actions_need_authorization_and_an_authorized_device() -> None:
    class UntrustedDevice:
        def __init__(self) -> None:
            self.device_info = {"model": "SYNTHETIC"}

        def app_current(self):
            return {"package": "com.ss.android.pda"}

        def dump_hierarchy(self, compressed=False):
            return read_fixture("single_empty.xml")

    adapter = AdbDeviceAdapter(
        serial="SYN-DEVICE-A",
        uiautomator_client=UntrustedDevice(),
    )
    unauthorized_controller = WmsController(
        adapter,
        warehouse="SYN-WAREHOUSE",
        real_submission_authorized=False,
    )
    with pytest.raises(SubmissionBlocked, match="explicit site authorization"):
        unauthorized_controller.open_bag(make_config())

    authorized_controller = WmsController(
        adapter,
        warehouse="SYN-WAREHOUSE",
        real_submission_authorized=True,
    )
    with pytest.raises(SubmissionBlocked, match="connected and authorized"):
        authorized_controller.open_bag(make_config())


def test_session_is_invalidated_when_device_app_generation_changes() -> None:
    adapter, controller, session = make_controller(read_fixture("bag_ready.xml"))
    adapter.launch_app("com.ss.android.pda")

    with pytest.raises(SubmissionBlocked, match="session changed"):
        controller.submit_once(
            make_item(),
            session,
            physical_check=make_check(make_item()),
            attempt_id="attempt-syn-1",
        )


def test_switch_bag_checks_back_navigation_without_closing_container() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        snapshots_after_enter=(
            read_fixture("bag_ready.xml"),
            read_fixture("bag_ready_2.xml"),
        ),
        snapshots_after_back=(
            read_fixture("bag_ready.xml"),
            read_fixture("single_empty.xml"),
        ),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    controller.open_bag(make_config())
    next_config = CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER_2",
        display_name="Synthetic carrier 2",
        aliases=(),
        sort_code="SYN_SORT_02",
        container_no="SYN_CONTAINER_02",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )

    session = controller.switch_bag(next_config, unresolved_submissions=0)

    assert session.container_no == "SYN_CONTAINER_02"
    assert session.sort_code == "SYN_SORT_02"
    assert [action for action in adapter.actions if action[:1] == ("press_key",)] == [
        ("press_key", "ENTER"),
        ("press_key", "BACK"),
        ("press_key", "BACK"),
        ("press_key", "ENTER"),
    ]


def test_switch_bag_is_blocked_when_attempt_is_unresolved() -> None:
    adapter, controller, _ = make_controller(read_fixture("bag_ready.xml"))
    with pytest.raises(SubmissionBlocked, match="unresolved"):
        controller.switch_bag(make_config(), unresolved_submissions=1)
    assert [action[0] for action in adapter.actions].count("press_key") == 1


def make_dynamic_controller(*, outcome_map=None, sort_code="SYN_SORT_01"):
    dynamic_config = CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER",
        display_name="Synthetic carrier",
        aliases=(),
        sort_code=sort_code,
        container_no="SYN_CONTAINER_01",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )
    adapter = FakeDeviceAdapter(
        warehouse="SYN-WAREHOUSE",
        carrier_configs=(dynamic_config,),
        outcome_map=outcome_map,
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    return adapter, controller, dynamic_config


def test_dynamic_simulator_runs_default_success_with_carrier_mapping() -> None:
    adapter, controller, config = make_dynamic_controller()
    session = controller.open_bag(config)
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-dynamic-1"
    )

    assert result.outcome is ScanOutcome.SUCCESS
    assert result.evidence.latest_tracking == item.tracking
    assert result.evidence.container_no == config.container_no
    assert result.evidence.sort_code == config.sort_code
    assert adapter.adb_write_call_count == 0


def test_dynamic_simulator_recovers_duplicate_deterministically() -> None:
    adapter, controller, config = make_dynamic_controller(
        outcome_map={"SYN_TRACK_NEW": "ALREADY_IN_BAG"}
    )
    session = controller.open_bag(config)
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-dynamic-2"
    )
    recovery = controller.recover_duplicate(result, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert result.evidence.logid
    assert recovery.recovered
    assert recovery.container_no == config.container_no
    assert adapter.adb_write_call_count == 0


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("REJECTED", ScanOutcome.REJECTED), ("UNKNOWN", ScanOutcome.UNKNOWN)],
)
def test_dynamic_simulator_supports_rejected_and_unknown_paths(outcome, expected) -> None:
    adapter, controller, config = make_dynamic_controller(
        outcome_map={"SYN_TRACK_NEW": outcome}
    )
    session = controller.open_bag(config)
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id=f"attempt-{outcome.lower()}"
    )

    assert result.outcome is expected
    assert result.attempt_id == f"attempt-{outcome.lower()}"
    assert adapter.adb_write_call_count == 0


def test_dynamic_simulator_reflects_actual_container_sort_mapping() -> None:
    adapter = FakeDeviceAdapter(
        warehouse="SYN-WAREHOUSE",
        container_sort_map={"SYN_CONTAINER_01": "SYN_SORT_ACTUAL"},
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    with pytest.raises(SubmissionBlocked, match="does not match configuration"):
        controller.open_bag(make_config())
    assert adapter.adb_write_call_count == 0


def test_dynamic_simulator_switches_between_configured_containers() -> None:
    first = make_config()
    second = CarrierConfig(
        carrier_key="SYNTHETIC_CARRIER_2",
        display_name="Synthetic carrier 2",
        aliases=(),
        sort_code="SYN_SORT_02",
        container_no="SYN_CONTAINER_02",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )
    adapter = FakeDeviceAdapter(
        warehouse="SYN-WAREHOUSE",
        carrier_configs=(first, second),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    controller.open_bag(first)

    session = controller.switch_bag(second, unresolved_submissions=0)

    assert session.container_no == second.container_no
    assert session.sort_code == second.sort_code
    assert adapter.adb_write_call_count == 0



class PolledFakeAdapter(FakeDeviceAdapter):
    """Static fake whose page changes over successive reads after the scan Enter."""

    def __init__(self, *args, polls_after_scan=(), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._polls_after_scan = list(polls_after_scan)
        self._scan_entered = False

    def press_key(self, key: str) -> None:
        super().press_key(key)
        if key.upper() == "ENTER" and self._entered_text[-1:] == ["SYN_TRACK_NEW"]:
            self._scan_entered = True

    def snapshot_xml(self):
        if self._scan_entered and self._polls_after_scan:
            self._current_xml = self._polls_after_scan.pop(0)
        return super().snapshot_xml()


def _previous_success_xml() -> str:
    return read_fixture("scan_success.xml").replace("SYN_TRACK_NEW", "SYN_TRACK_PREV")


def _previous_rejected_xml(logid: str) -> str:
    return _previous_success_xml().replace(
        'text="扫描成功" visible-to-user="true" enabled="true" bounds="[0,360][150,400]" '
        'resource-id="success_status" />',
        'text="扫描失败" visible-to-user="true" enabled="true" bounds="[0,360][150,400]" '
        'resource-id="rejected_status" />\n  <node package="com.ss.android.pda" '
        f'class="android.widget.TextView" text="Logid: {logid}" visible-to-user="true" '
        'enabled="true" bounds="[0,410][300,450]" resource-id="log_id" />',
    )


def make_polled_controller(before_xml: str, *polls: str):
    adapter = PolledFakeAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        devices=(),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
        polls_after_scan=polls,
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE", poll_interval_seconds=0)
    session = controller.open_bag(make_config())
    adapter._current_xml = before_xml
    return adapter, controller, session


def test_result_polling_waits_past_the_previous_items_result_page() -> None:
    previous = _previous_success_xml()
    adapter, controller, session = make_polled_controller(
        previous, previous, read_fixture("scan_success.xml")
    )
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-stale"
    )

    assert result.outcome is ScanOutcome.SUCCESS
    assert [action for action in adapter.actions if action[0] == "press_key"].count(
        ("press_key", "ENTER")
    ) == 2


def test_rejection_for_another_tracking_is_unknown_not_rejected() -> None:
    _, controller, session = make_polled_controller(
        _previous_rejected_xml("SYN-LOG-OLD"), _previous_rejected_xml("SYN-LOG-NEW")
    )
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-other-reject"
    )

    assert result.outcome is ScanOutcome.UNKNOWN


def test_duplicate_requires_exact_dialog_text() -> None:
    original = read_fixture("duplicate_dialog.xml")
    altered = original.replace(
        'text="已扫描包裹已在当前大包中。', 'text="已扫描包裹已在当前大包中（其他大包）。'
    )
    assert altered != original
    _, controller, session = make_controller(read_fixture("bag_ready.xml"), altered)
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-dup-text"
    )

    assert result.outcome is ScanOutcome.UNKNOWN


def test_success_page_with_error_dialog_is_unknown() -> None:
    with_error = read_fixture("scan_success.xml").replace(
        "</hierarchy>",
        '  <node package="com.ss.android.pda" class="android.widget.TextView" '
        'text="SYN_TRACK_NEW 异常" visible-to-user="true" enabled="true" '
        'bounds="[0,410][300,450]" resource-id="error_text" />\n</hierarchy>',
    )
    _, controller, session = make_controller(read_fixture("bag_ready.xml"), with_error)
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-error-dialog"
    )

    assert result.outcome is ScanOutcome.UNKNOWN


def test_result_polling_stops_at_monotonic_deadline(monkeypatch) -> None:
    import tt_auto_bag_scanner.infrastructure.wms_controller as module

    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        devices=(),
        snapshots_after_enter=(read_fixture("bag_ready.xml"), read_fixture("bag_ready.xml")),
    )
    controller = WmsController(
        adapter,
        warehouse="SYN-WAREHOUSE",
        poll_attempts=30,
        poll_interval_seconds=0,
        timeout_seconds=1.0,
    )
    session = controller.open_bag(make_config())
    clock = iter(float(value * 100) for value in range(1000))
    monkeypatch.setattr(module, "monotonic", lambda: next(clock), raising=False)
    item = make_item()
    reads_before = [a[0] for a in adapter.actions].count("snapshot_xml")

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-deadline"
    )

    reads = [a[0] for a in adapter.actions].count("snapshot_xml") - reads_before
    assert result.outcome is ScanOutcome.UNKNOWN
    assert reads < 10


def _without_warehouse(name: str) -> str:
    return "\n".join(
        line for line in read_fixture(name).splitlines() if "warehouse_" not in line
    )


def test_warehouse_verified_on_home_covers_pages_without_warehouse_label() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("home.xml"),
        snapshots_after_tap=(
            _without_warehouse("container_menu.xml"),
            _without_warehouse("single_empty.xml"),
        ),
        snapshots_after_enter=(
            _without_warehouse("bag_ready.xml"),
            _without_warehouse("scan_success.xml"),
        ),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    session = controller.open_bag(make_config())
    item = make_item()

    result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-home-wh"
    )

    assert result.outcome is ScanOutcome.SUCCESS


def test_page_without_warehouse_label_requires_prior_home_verification() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=_without_warehouse("single_empty.xml"),
        devices=(),
        snapshots_after_enter=(_without_warehouse("bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")

    with pytest.raises(SubmissionBlocked):
        controller.open_bag(make_config())

    assert adapter.entered_text == ()


def test_home_with_other_warehouse_never_navigates() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("home.xml"))
    controller = WmsController(adapter, warehouse="SYN-OTHER")

    with pytest.raises(SubmissionBlocked):
        controller.open_bag(make_config())

    assert [a for a in adapter.actions if a[0] == "tap"] == []


# --- 真机“单排序”页面（脱敏 XML）的离线回归 ------------------------------------------

REAL_TRACKING = "TRACKING_TEST_001"
REAL_CONTAINER = "CT0000000000001"
REAL_SORT = "CARRIER_TEST_01"


def _focus_single_input(xml: str) -> str:
    focused, count = _re.subn(
        r'(class="android\.widget\.EditText"[^>]*?)focused="false"',
        r'\1focused="true"',
        xml,
    )
    assert count == 1
    return focused


def _real_config() -> CarrierConfig:
    return CarrierConfig(
        carrier_key="CARRIER_TEST",
        display_name="Carrier test",
        aliases=(),
        sort_code=REAL_SORT,
        container_no=REAL_CONTAINER,
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )


def _real_item(tracking: str = REAL_TRACKING) -> QueueItem:
    return QueueItem(
        id="item-real-1",
        task_id="task-real-1",
        tracking=tracking,
        carrier_key="CARRIER_TEST",
        sort_code=REAL_SORT,
        container_no=REAL_CONTAINER,
        outbound_at_utc=NOW,
        source_row_numbers=(2,),
        status=ItemStatus.READY,
    )


def make_real_controller(*after_enter: str, before: str | None = None, extra_taps=()):
    before_xml = before or read_fixture("real_single_sort_bag_ready.xml")
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_wms_home.xml"),
        devices=(),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
            _focus_single_input(before_xml) if before is None else before_xml,
            *extra_taps,
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"), *after_enter),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)
    session = controller.open_bag(_real_config())
    if before is not None:
        adapter._current_xml = before
    return adapter, controller, session


def _submit_real(controller, session, item=None):
    item = item or _real_item()
    return controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="attempt-real-1"
    )


def test_real_open_bag_reads_label_adjacent_container_and_sort() -> None:
    adapter, _, session = make_real_controller()

    assert session.container_no == REAL_CONTAINER
    assert session.sort_code == REAL_SORT
    assert adapter.entered_text == (REAL_CONTAINER,)


def test_real_success_uses_first_latest_record_and_verified_session() -> None:
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_success.xml")
    )

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.SUCCESS
    assert result.evidence.latest_tracking == REAL_TRACKING
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING)
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_success_without_tracking_in_visible_latest_records_is_unknown() -> None:
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_success_scrolled.xml")
    )

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.UNKNOWN
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_success_with_other_sort_code_is_unknown() -> None:
    other_sort = read_fixture("real_single_sort_success.xml").replace(REAL_SORT, "CARRIER_TEST_02")
    _, controller, session = make_real_controller(other_sort)

    assert _submit_real(controller, session).outcome is ScanOutcome.UNKNOWN


def test_real_success_with_visible_error_text_is_unknown() -> None:
    with_error = read_fixture("real_single_sort_success.xml").replace(
        "</hierarchy>",
        '<node index="9" text="已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID" resource-id="" '
        'class="android.widget.TextView" package="com.ss.android.pda" content-desc="" '
        'enabled="true" focused="false" visible-to-user="true" bounds="[54,525][414,624]" />'
        "</hierarchy>",
    )
    _, controller, session = make_real_controller(with_error)

    assert _submit_real(controller, session).outcome is ScanOutcome.UNKNOWN


def test_real_tracking_already_first_before_submit_is_blocked() -> None:
    success = read_fixture("real_single_sort_success.xml")
    adapter, controller, session = make_real_controller(before=success)

    with pytest.raises(SubmissionBlocked):
        _submit_real(controller, session)
    assert adapter.entered_text == (REAL_CONTAINER,)


def test_real_duplicate_recovery_backs_out_without_retyping_tracking() -> None:
    duplicate = read_fixture("real_single_sort_duplicate.xml")
    adapter, controller, session = make_real_controller(duplicate)
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    result = _submit_real(controller, session)
    recovery = controller.recover_duplicate(result, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert result.evidence.logid == "TEST_LOG_ID"
    assert recovery.recovered
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING)
    assert adapter.actions.count(("press_key", "BACK")) == 1


def test_real_duplicate_for_another_tracking_is_unknown() -> None:
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_duplicate.xml")
    )

    result = _submit_real(controller, session, _real_item("TRACKING_TEST_009"))

    assert result.outcome is ScanOutcome.UNKNOWN


def test_grouping_status_dialog_is_dismissed_with_back_and_skipped() -> None:
    dialog = read_fixture("real_single_sort_duplicate_dialog.xml").replace(
        "已扫描包裹已在当前大包中。", "状态异常，状态为组包中"
    )
    adapter, controller, session = make_real_controller(dialog)
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    result = _submit_real(controller, session)

    assert result.skip_rejected_item
    assert result.outcome is ScanOutcome.REJECTED
    assert adapter.actions.count(("press_key", "BACK")) == 1
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING)


def test_real_duplicate_recovery_leaves_non_empty_dialog_input_untouched() -> None:
    duplicate = read_fixture("real_single_sort_duplicate.xml")
    filled, count = _re.subn(
        r'text=""(\s+resource-id=""\s+class="android\.widget\.EditText"[^>]*'
        r'bounds="\[42,597\]\[438,670\]")',
        rf'text="{REAL_CONTAINER}"\1',
        duplicate,
    )
    assert count == 1
    adapter, controller, session = make_real_controller(duplicate)
    result = _submit_real(controller, session)
    adapter._current_xml = filled
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    assert controller.recover_duplicate(result, session).recovered
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING)


def test_verify_surfaces_screen_off_message() -> None:
    from tt_auto_bag_scanner.infrastructure.exception_handler import ScreenOffError

    class ScreenOffAdapter(FakeDeviceAdapter):
        def snapshot_xml(self):  # type: ignore[override]
            raise ScreenOffError("PDA 屏幕已关闭，请唤醒并解锁后重试")

    controller = WmsController(ScreenOffAdapter(), warehouse="SYN-WAREHOUSE")
    with pytest.raises(ScreenOffError, match="屏幕已关闭"):
        controller.verify_current_or_open_bag(make_config())


def test_verify_unknown_page_message_is_actionable_chinese() -> None:
    adapter = FakeDeviceAdapter()
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE")
    from tt_auto_bag_scanner.domain.models import PageObservation, PageState

    controller.observe = lambda: PageObservation(state=PageState.UNKNOWN)  # type: ignore[method-assign]
    with pytest.raises(UnknownPageError, match="无法识别"):
        controller.verify_current_or_open_bag(make_config())


class _SlowLoadingAdapter(FakeDeviceAdapter):
    """After each tap, the next page shows an unknown loading page for a few reads."""

    def __init__(self, *args, loading_reads: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._loading_reads = loading_reads
        self._remaining = 0
        self._loading_xml = (
            '<hierarchy><node package="com.ss.android.pda" class="android.widget.FrameLayout"'
            ' visible-to-user="true" bounds="[0,0][100,100]" text="" resource-id=""/></hierarchy>'
        )

    def tap(self, x: int, y: int) -> None:
        super().tap(x, y)
        self._remaining = self._loading_reads

    def tap_field(self, field) -> None:  # type: ignore[override]
        super().tap_field(field)
        self._remaining = 0

    def snapshot_xml(self):  # type: ignore[override]
        real = super().snapshot_xml()
        if self._remaining > 0:
            self._remaining -= 1
            return type(real)(
                xml=self._loading_xml,
                captured_at_utc=real.captured_at_utc,
                package_name=real.package_name,
                activity_name=real.activity_name,
            )
        return real


def _slow_adapter(loading_reads: int) -> _SlowLoadingAdapter:
    return _SlowLoadingAdapter(
        initial_xml=read_fixture("home.xml"),
        snapshots_after_tap=(
            read_fixture("container_menu.xml"),
            read_fixture("single_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
        loading_reads=loading_reads,
    )


def test_navigation_waits_for_slow_page_load() -> None:
    adapter = _slow_adapter(loading_reads=2)
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(make_config())

    assert session.container_no == "SYN_CONTAINER_01"
    assert len([action for action in adapter.actions if action[0] == "tap"]) == 3


def test_navigation_times_out_with_chinese_message_without_extra_taps() -> None:
    adapter = _slow_adapter(loading_reads=100)
    controller = WmsController(
        adapter, warehouse="SYN-WAREHOUSE", poll_attempts=3, poll_interval_seconds=0
    )

    with pytest.raises(UnknownPageError, match="页面未加载到"):
        controller.verify_current_or_open_bag(make_config())
    assert len([action for action in adapter.actions if action[0] == "tap"]) == 1


class _SlowBagOpenAdapter(_SlowLoadingAdapter):
    """Navigation is instant; the bag page loads slowly after Enter."""

    def tap(self, x: int, y: int) -> None:
        FakeDeviceAdapter.tap(self, x, y)

    def press_key(self, key: str) -> None:
        super().press_key(key)
        if key.upper() == "ENTER":
            self._remaining = self._loading_reads


def test_bag_open_waits_for_slow_bag_page() -> None:
    adapter = _SlowBagOpenAdapter(
        initial_xml=read_fixture("single_empty.xml"),
        snapshots_after_enter=(read_fixture("bag_ready.xml"),),
        loading_reads=2,
    )
    controller = WmsController(adapter, warehouse="SYN-WAREHOUSE", poll_interval_seconds=0)
    controller._warehouse_verified_for = adapter.session_generation

    session = controller.open_bag(make_config())

    assert session.container_no == "SYN_CONTAINER_01"
    assert [a for a in adapter.actions if a[0] == "press_key"] == [("press_key", "ENTER")]


def _real_config() -> CarrierConfig:
    return CarrierConfig(
        carrier_key="SYN_REAL",
        display_name="Synthetic real",
        aliases=(),
        sort_code="CARRIER_TEST_01",
        container_no="CT0000000000001",
        enabled=True,
        version=1,
        updated_at_utc=NOW,
    )


def test_verify_returns_home_to_check_warehouse_then_reopens_bag() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_bag_ready.xml"),
        snapshots_after_back=(
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_containerization.xml"),
            read_fixture("real_wms_home.xml"),
        ),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config())

    assert session.container_no == "CT0000000000001"
    assert [a for a in adapter.actions if a[0] == "press_key"].count(("press_key", "BACK")) == 3


def test_return_home_stops_on_unknown_page_without_more_back() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_bag_ready.xml"),
        snapshots_after_back=(read_fixture("unknown.xml"),),
    )
    controller = WmsController(
        adapter, warehouse="WAREHOUSE_TEST", poll_attempts=2, poll_interval_seconds=0
    )

    with pytest.raises(UnknownPageError, match="返回首页"):
        controller.verify_current_or_open_bag(_real_config())
    assert [a for a in adapter.actions if a[0] == "press_key"] == [("press_key", "BACK")]


def test_active_session_is_none_after_invalidation() -> None:
    controller = WmsController.__new__(WmsController)
    controller._session = None
    controller._session_generation = None
    assert controller.active_session is None


def _other_bag(xml: str) -> str:
    return xml.replace("CT0000000000001", "CT0000000000009").replace(
        "CARRIER_TEST_01", "CARRIER_TEST_09"
    )


def test_verify_backs_out_of_other_bag_then_opens_configured_bag() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=_other_bag(read_fixture("real_single_sort_bag_ready.xml")),
        snapshots_after_back=(
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_containerization.xml"),
            read_fixture("real_wms_home.xml"),
        ),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert session.container_no == "CT0000000000001"
    keys = [a for a in adapter.actions if a[0] == "press_key"]
    assert keys.count(("press_key", "BACK")) == 3


def test_verify_never_backs_out_of_other_bag_with_unresolved_submissions() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=_other_bag(read_fixture("real_single_sort_bag_ready.xml")),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    with pytest.raises(SubmissionBlocked, match="container"):
        controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=1)
    assert not any(a[0] in {"tap", "enter_text", "press_key"} for a in adapter.actions)


def test_verify_backs_out_of_failed_open_page_then_opens_bag() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_open_failed.xml"),
        snapshots_after_back=(
            read_fixture("real_single_sort_open_failed.xml"),
            read_fixture("real_containerization.xml"),
            read_fixture("real_wms_home.xml"),
        ),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert session.container_no == "CT0000000000001"
    assert not any(a[0] == "enter_text" and "CT0000000000009" in str(a) for a in adapter.actions)


_IME_NODE = (
    '<node index="0" text="Q" resource-id="" class="android.widget.TextView" '
    'package="com.google.android.inputmethod.latin" content-desc="Q" checkable="false" '
    'checked="false" clickable="true" enabled="true" focusable="false" focused="false" '
    'scrollable="false" long-clickable="false" password="false" selected="false" '
    'visible-to-user="true" bounds="[0,500][48,560]" drawing-order="0" hint="" display-id="0" />'
)


def _with_keyboard(xml: str) -> str:
    return xml.replace("</hierarchy>", _IME_NODE + "</hierarchy>")


def test_real_submit_taps_outside_keyboard_and_refocuses_before_typing() -> None:
    focused = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    unfocused = read_fixture("real_single_sort_bag_ready.xml")
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_success.xml"), before=_with_keyboard(focused)
    )
    adapter._next_tap_snapshots = [_with_keyboard(focused), unfocused, focused]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.SUCCESS
    assert ("press_key", "BACK") not in adapter.actions
    outside_tap = adapter.actions.index(("tap", 64, 126))
    text_entry = adapter.actions.index(("enter_text", REAL_TRACKING))
    assert adapter.actions[outside_tap + 1 : text_entry] == [
        ("snapshot_xml",),
        ("tap", 239, 675),
        ("snapshot_xml",),
    ]
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_submit_never_enters_while_keyboard_stays_visible() -> None:
    keyboard = _with_keyboard(_focus_single_input(read_fixture("real_single_sort_bag_ready.xml")))
    adapter, controller, session = make_real_controller(before=keyboard)
    adapter._next_tap_snapshots = [keyboard, keyboard]

    with pytest.raises(SubmissionBlocked, match="keyboard"):
        _submit_real(controller, session)
    assert adapter.actions.count(("press_key", "ENTER")) == 1
    assert ("press_key", "BACK") not in adapter.actions
    assert REAL_TRACKING not in adapter.entered_text


def test_real_submit_stops_if_outside_tap_reveals_a_different_bag() -> None:
    focused = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    keyboard = _with_keyboard(focused)
    adapter, controller, session = make_real_controller(before=keyboard)
    adapter._next_tap_snapshots = [
        keyboard,
        focused.replace(REAL_CONTAINER, "CT0000000000002"),
    ]

    with pytest.raises(SubmissionBlocked, match="visible container does not match"):
        _submit_real(controller, session)
    assert adapter.actions.count(("press_key", "ENTER")) == 1
    assert REAL_TRACKING not in adapter.entered_text


def test_real_result_hidden_by_keyboard_is_reread_after_dismissing_it() -> None:
    hidden = _with_keyboard(read_fixture("real_single_sort_bag_ready.xml"))
    adapter, controller, session = make_real_controller(hidden)
    adapter._current_xml = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    adapter._next_tap_snapshots = [
        _focus_single_input(read_fixture("real_single_sort_bag_ready.xml")),
        read_fixture("real_single_sort_success.xml"),
    ]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.SUCCESS
    assert adapter.actions.count(("press_key", "ENTER")) == 2
    assert ("press_key", "BACK") not in adapter.actions
    assert [a for a in adapter.actions if a[0] == "tap"][-1] == ("tap", 64, 126)
    assert controller.active_session is session


def test_real_scan_continues_after_keyboard_temporarily_hides_page_identity() -> None:
    ready = read_fixture("real_single_sort_bag_ready.xml")
    hidden = _with_keyboard(ready.replace('text="单排序"', 'text=""'))
    success = read_fixture("real_single_sort_success.xml")
    second_tracking = "TESTTRACKING000002"
    adapter, controller, session = make_real_controller(
        hidden, success.replace(REAL_TRACKING, second_tracking)
    )
    adapter._next_tap_snapshots = [_focus_single_input(ready), success]

    assert _submit_real(controller, session).outcome is ScanOutcome.SUCCESS
    assert controller.verify_current_or_open_bag(_real_config()) is session
    adapter._next_tap_snapshots = [_focus_single_input(success)]
    second = replace(_real_item(), id="item-real-2", tracking=second_tracking)
    result = controller.submit_once(second, session, make_check(second), "attempt-real-2")

    assert result.outcome is ScanOutcome.SUCCESS
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING, second_tracking)
    assert adapter.actions.count(("press_key", "ENTER")) == 3


@pytest.mark.parametrize("invalid_page", ["login", "other_bag"])
def test_keyboard_result_does_not_restore_an_invalid_session(invalid_page: str) -> None:
    ready = read_fixture("real_single_sort_bag_ready.xml")
    if invalid_page == "login":
        invalid = ready.replace('text="单排序"', 'text="登录"')
    else:
        invalid = ready.replace('text="单排序"', 'text=""').replace(
            REAL_CONTAINER, "CT0000000000002"
        )
    adapter, controller, session = make_real_controller(_with_keyboard(invalid))
    adapter._next_tap_snapshots = [
        _focus_single_input(ready),
        read_fixture("real_single_sort_success.xml"),
    ]

    assert _submit_real(controller, session).outcome is ScanOutcome.UNKNOWN
    assert controller.active_session is None
    with pytest.raises(SubmissionBlocked, match="blocked"):
        controller.verify_current_or_open_bag(_real_config())
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_missing_result_without_keyboard_stays_unknown_without_back() -> None:
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_bag_ready.xml")
    )

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.UNKNOWN
    assert ("press_key", "BACK") not in adapter.actions
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_submit_tolerates_layout_shift_while_keyboard_is_open() -> None:
    focused = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    match = _re.search(r'(class="android\.widget\.EditText"[^>]*?bounds=")\[(\d+),(\d+)\]', focused)
    assert match is not None
    shifted = focused.replace(
        f"{match.group(1)}[{match.group(2)},{match.group(3)}]",
        f"{match.group(1)}[{match.group(2)},{int(match.group(3)) - 40}]",
        1,
    )
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_success.xml")
    )
    adapter._current_xml = focused
    adapter._next_tap_snapshots = [_with_keyboard(shifted), shifted]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.SUCCESS
    assert adapter.entered_text[-1] == REAL_TRACKING


@pytest.mark.parametrize("changes_id", [False, True])
def test_real_submit_uses_current_focused_input_after_layout_changes(changes_id: bool) -> None:
    ready = read_fixture("real_single_sort_bag_ready.xml")
    focused = _focus_single_input(ready)
    shifted = focused.replace("[34,639][445,712]", "[34,579][445,652]")
    assert shifted != focused
    if changes_id:
        marker = 'resource-id="" class="android.widget.EditText"'
        focused = focused.replace(marker, 'resource-id="test_before" class="android.widget.EditText"')
        shifted = shifted.replace(marker, 'resource-id="test_after" class="android.widget.EditText"')
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_success.xml")
    )
    adapter._current_xml = focused
    adapter._next_tap_snapshots = [shifted]

    assert _submit_real(controller, session).outcome is ScanOutcome.SUCCESS
    assert adapter.entered_text.count(REAL_TRACKING) == 1
    assert ("tap", 239, 615) not in adapter.actions
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_real_unclear_dialog_with_keyboard_is_never_closed_by_back() -> None:
    dialog = _with_keyboard(
        read_fixture("real_single_sort_duplicate.xml").replace(
            "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_001",
            "请重新扫描追踪号码，然后扫描正确的容器 TRACKING_TEST_999",
        )
    )
    adapter, controller, session = make_real_controller(dialog)

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.UNKNOWN
    assert ("press_key", "BACK") not in adapter.actions


def _without_dialog_message(xml: str) -> str:
    stripped, count = _re.subn(
        r'<node [^>]*text="已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID,[^"]*"[^>]*/>', "", xml
    )
    assert count == 1
    return stripped


def test_real_dialog_hidden_by_keyboard_is_reread_after_tapping_its_title() -> None:
    dialog = read_fixture("real_single_sort_duplicate_dialog.xml")
    adapter, controller, session = make_real_controller(
        _with_keyboard(_without_dialog_message(dialog))
    )
    adapter._current_xml = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    adapter._next_tap_snapshots = [
        _focus_single_input(read_fixture("real_single_sort_bag_ready.xml")),
        dialog,
    ]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert ("press_key", "BACK") not in adapter.actions
    assert [a for a in adapter.actions if a[0] == "tap"][-1] == ("tap", 54, 306)


def test_real_duplicate_returns_past_keyboard_and_submits_next_item() -> None:
    dialog = read_fixture("real_single_sort_duplicate_dialog.xml")
    ready = read_fixture("real_single_sort_bag_ready.xml")
    next_tracking = "TRACKING_TEST_009"
    success = read_fixture("real_single_sort_success.xml").replace(REAL_TRACKING, next_tracking)
    adapter, controller, session = make_real_controller(_with_keyboard(dialog), success)
    adapter._next_back_snapshots = [dialog, ready]
    adapter._next_tap_snapshots.extend([_focus_single_input(ready)])

    result = _submit_real(controller, session)
    recovery = controller.recover_duplicate(result, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert recovery.recovered
    assert adapter.actions.count(("press_key", "BACK")) == 2
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING)
    item = _real_item(next_tracking)
    next_result = controller.submit_once(
        item, session, physical_check=make_check(item), attempt_id="next-attempt"
    )
    assert next_result.outcome is ScanOutcome.SUCCESS
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING, next_tracking)


def test_real_unknown_result_does_not_latch_controller_and_bag_can_be_reverified() -> None:
    adapter, controller, session = make_real_controller(
        read_fixture("real_single_sort_bag_ready.xml")
    )

    result = _submit_real(controller, session)
    adapter._current_xml = read_fixture("real_single_sort_bag_ready.xml")
    reverified = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert result.outcome is ScanOutcome.UNKNOWN
    assert controller.active_session is reverified
    assert reverified.session_id != session.session_id


def test_abandoned_duplicate_recovery_unlatches_controller_for_reverify() -> None:
    dialog = read_fixture("real_single_sort_duplicate_dialog.xml")
    adapter, controller, session = make_real_controller(dialog)
    result = _submit_real(controller, session)
    assert result.outcome is ScanOutcome.ALREADY_IN_BAG

    controller.abandon_duplicate_recovery()
    adapter._current_xml = read_fixture("real_single_sort_bag_ready.xml")
    reverified = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert controller.active_session is reverified


def test_unreadable_scan_result_allows_next_item_after_fresh_verification(monkeypatch) -> None:
    ready = read_fixture("real_single_sort_bag_ready.xml")
    next_tracking = "TESTTRACKING000002"
    success = read_fixture("real_single_sort_success.xml").replace(REAL_TRACKING, next_tracking)
    adapter, controller, session = make_real_controller(read_fixture("unknown.xml"), ready, success)
    adapter._next_back_snapshots = [
        read_fixture("real_single_sort_empty.xml"),
        read_fixture("real_containerization.xml"),
        read_fixture("real_wms_home.xml"),
    ]

    assert _submit_real(controller, session).outcome is ScanOutcome.UNKNOWN
    assert controller.active_session is None
    snapshot_xml = adapter.snapshot_xml

    def read_then_restore_bag():
        snapshot = snapshot_xml()
        adapter._current_xml = ready
        monkeypatch.setattr(adapter, "snapshot_xml", snapshot_xml)
        return snapshot

    monkeypatch.setattr(adapter, "snapshot_xml", read_then_restore_bag)
    adapter._next_tap_snapshots = [
        read_fixture("real_containerization.xml"),
        read_fixture("real_single_sort_empty.xml"),
        read_fixture("real_single_sort_empty.xml"),
    ]
    reverified = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)
    adapter._next_tap_snapshots = [_focus_single_input(ready)]
    item = replace(_real_item(), id="item-real-2", tracking=next_tracking)

    assert reverified.session_id != session.session_id
    result = controller.submit_once(item, reverified, make_check(item), "attempt-real-2")
    assert result.outcome is ScanOutcome.SUCCESS
    assert adapter.entered_text == (REAL_CONTAINER, REAL_TRACKING, REAL_CONTAINER, next_tracking)
    assert adapter.actions.count(("press_key", "ENTER")) == 4


@pytest.mark.parametrize("keyboard_closes", [True, False])
def test_preflight_dismisses_keyboard_hiding_container_before_verifying(
    keyboard_closes: bool,
) -> None:
    ready = _focus_single_input(read_fixture("real_single_sort_bag_ready.xml"))
    obscured = _with_keyboard(
        ready.replace('text="大包号"', 'text=""').replace(f'text="{REAL_CONTAINER}"', 'text=""')
    )
    warehouse = "\n".join(read_fixture("bag_ready.xml").splitlines()[2:4])
    visible = ready.replace(
        "</hierarchy>", warehouse.replace("SYN-WAREHOUSE", "WAREHOUSE_TEST") + "</hierarchy>"
    )
    after_back = visible if keyboard_closes else obscured
    adapter = FakeDeviceAdapter(initial_xml=obscured, snapshots_after_back=(after_back,))
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    if keyboard_closes:
        session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)
        assert session.container_no == REAL_CONTAINER
    else:
        with pytest.raises(SubmissionBlocked, match="keyboard is still visible"):
            controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert [action for action in adapter.actions if action[0] == "press_key"] == [
        ("press_key", "BACK")
    ]


def test_verify_clears_inline_grouping_error_input_then_issues_session() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_grouping_inline.xml"),
        snapshots_after_clear=(read_fixture("real_single_sort_bag_ready.xml"),),
        snapshots_after_back=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_wms_home.xml"),
        ),
        snapshots_after_tap=(
            read_fixture("real_single_sort_grouping_inline.xml"),
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert session.container_no == "CT0000000000001"
    assert ("clear_text",) in adapter.actions
    assert "TRACKING_TEST_009" not in adapter.entered_text


def test_verify_keeps_inline_grouping_input_for_other_sort() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_grouping_inline.xml").replace(
            "CARRIER_TEST_01", "CARRIER_OTHER_01"
        ),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    with pytest.raises(Exception):
        controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)
    assert ("clear_text",) not in adapter.actions


def test_verify_keeps_inline_grouping_input_with_unresolved_submission() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_single_sort_grouping_inline.xml"),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    with pytest.raises(Exception):
        controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=1)
    assert ("clear_text",) not in adapter.actions


def _real_grouping_dialog() -> str:
    return read_fixture("real_single_sort_grouping_dialog.xml").replace(
        "TRACKING_TEST_010", REAL_TRACKING
    ).replace("CARRIER_TEST_01", REAL_SORT).replace("CT0000000000001", REAL_CONTAINER)


def test_real_grouping_dialog_is_backed_out_and_skipped() -> None:
    adapter, controller, session = make_real_controller(_real_grouping_dialog())
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert adapter.actions.count(("press_key", "BACK")) == 1


def test_grouping_dialog_hidden_behind_keyboard_is_skipped_after_reread() -> None:
    adapter, controller, session = make_real_controller(
        _with_keyboard(_real_grouping_dialog()),
        extra_taps=(_real_grouping_dialog(),),
    )
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert adapter.actions.count(("press_key", "BACK")) == 1
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_grouping_dialog_rendered_after_inline_error_is_skipped() -> None:
    dialog = _real_grouping_dialog()
    inline = read_fixture("real_single_sort_grouping_inline.xml").replace(
        "TEST_GROUPING_LOG_ID", "TEST_GROUPING_DIALOG_LOG"
    ).replace("TRACKING_TEST_009", REAL_TRACKING).replace(
        "CARRIER_TEST_01", REAL_SORT
    ).replace("CT0000000000001", REAL_CONTAINER)
    adapter, controller, session = make_real_controller(inline)
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]
    original = adapter.snapshot_xml
    reads = {"after_enter": 0}

    def snapshot_xml():
        if adapter._current_xml == inline:
            reads["after_enter"] += 1
            if reads["after_enter"] > 1:
                adapter._current_xml = dialog
        return original()

    adapter.snapshot_xml = snapshot_xml

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert adapter.actions.count(("press_key", "ENTER")) == 2
    assert adapter.actions.count(("press_key", "BACK")) == 1


def _delayed(adapter, first: str, then: str) -> None:
    original = adapter.snapshot_xml
    reads = {"n": 0}

    def snapshot_xml():
        if adapter._current_xml == first:
            reads["n"] += 1
            if reads["n"] > 1:
                adapter._current_xml = then
        return original()

    adapter.snapshot_xml = snapshot_xml


def test_duplicate_dialog_rendered_after_inline_error_is_already_in_bag() -> None:
    dialog = read_fixture("real_single_sort_duplicate.xml")
    inline = read_fixture("real_single_sort_grouping_inline.xml").replace(
        "状态异常，状态为组包中; Logid=TEST_GROUPING_LOG_ID",
        "已扫描包裹已在当前大包中。; Logid=TEST_LOG_ID",
    ).replace("TRACKING_TEST_009", REAL_TRACKING)
    adapter, controller, session = make_real_controller(inline)
    _delayed(adapter, inline, dialog)

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.ALREADY_IN_BAG
    assert adapter.actions.count(("press_key", "ENTER")) == 2


def test_verify_closes_leftover_grouping_dialog_with_back() -> None:
    dialog = read_fixture("real_single_sort_grouping_dialog.xml")
    adapter = FakeDeviceAdapter(
        initial_xml=dialog,
        snapshots_after_back=(
            read_fixture("real_single_sort_bag_ready.xml"),
            read_fixture("real_containerization.xml"),
            read_fixture("real_wms_home.xml"),
        ),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert session.container_no == "CT0000000000001"
    assert "TRACKING_TEST_010" not in adapter.entered_text


def test_verify_keeps_leftover_dialog_with_unresolved_submission() -> None:
    adapter = FakeDeviceAdapter(initial_xml=read_fixture("real_single_sort_grouping_dialog.xml"))
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    with pytest.raises(Exception):
        controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=1)
    assert ("press_key", "BACK") not in adapter.actions


def test_grouping_dialog_revealed_by_keyboard_tap_is_skipped() -> None:
    inline = read_fixture("real_single_sort_grouping_inline.xml").replace(
        "TEST_GROUPING_LOG_ID", "TEST_GROUPING_DIALOG_LOG"
    ).replace("TRACKING_TEST_009", REAL_TRACKING).replace(
        "CARRIER_TEST_01", REAL_SORT
    ).replace("CT0000000000001", REAL_CONTAINER)
    adapter, controller, session = make_real_controller(
        _with_keyboard(inline), extra_taps=(_real_grouping_dialog(),)
    )
    adapter._next_back_snapshots = [read_fixture("real_single_sort_bag_ready.xml")]

    result = _submit_real(controller, session)

    assert result.outcome is ScanOutcome.REJECTED
    assert result.skip_rejected_item
    assert adapter.actions.count(("press_key", "ENTER")) == 2
    assert adapter.actions.count(("press_key", "BACK")) == 1


def test_open_bag_swipes_home_until_container_entry_is_visible() -> None:
    adapter = FakeDeviceAdapter(
        initial_xml=read_fixture("real_wms_home_inbound_expanded.xml"),
        snapshots_after_swipe=(read_fixture("real_wms_home.xml"),),
        snapshots_after_tap=(
            read_fixture("real_containerization.xml"),
            read_fixture("real_single_sort_empty.xml"),
            read_fixture("real_single_sort_empty.xml"),
        ),
        snapshots_after_enter=(read_fixture("real_single_sort_bag_ready.xml"),),
    )
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    session = controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)

    assert session.container_no == "CT0000000000001"
    swipes = [a for a in adapter.actions if a[0] == "swipe"]
    assert len(swipes) == 1
    _, x1, y1, x2, y2 = swipes[0]
    assert x1 == x2 and y1 > y2


def test_open_bag_gives_up_after_bounded_swipes() -> None:
    home = read_fixture("real_wms_home_inbound_expanded.xml")
    adapter = FakeDeviceAdapter(initial_xml=home)
    controller = WmsController(adapter, warehouse="WAREHOUSE_TEST", poll_interval_seconds=0)

    with pytest.raises(UnknownPageError):
        controller.verify_current_or_open_bag(_real_config(), unresolved_submissions=0)
    assert 1 <= sum(a[0] == "swipe" for a in adapter.actions) <= 6
    assert not any(a[0] == "tap" for a in adapter.actions)
