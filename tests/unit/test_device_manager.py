from __future__ import annotations

import pytest

from tt_auto_bag_scanner.domain.models import DeviceInfo
from tt_auto_bag_scanner.infrastructure.device_manager import (
    DeviceManager,
    DeviceSelectionRequired,
    DeviceUnavailable,
)
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter


def test_multiple_devices_require_explicit_selection() -> None:
    adapter = FakeDeviceAdapter(
        devices=(
            DeviceInfo("SYN-DEVICE-A", state="device", authorized=True),
            DeviceInfo("SYN-DEVICE-B", state="device", authorized=True),
        )
    )
    manager = DeviceManager(adapter)

    assert len(manager.refresh()) == 2
    with pytest.raises(DeviceSelectionRequired):
        manager.connect()


def test_offline_or_unauthorized_device_cannot_connect() -> None:
    adapter = FakeDeviceAdapter(
        devices=(
            DeviceInfo("SYN-OFFLINE", state="offline", authorized=False),
            DeviceInfo("SYN-UNAUTHORIZED", state="unauthorized", authorized=False),
        )
    )
    manager = DeviceManager(adapter)
    manager.refresh()

    manager.select("SYN-OFFLINE")
    with pytest.raises(DeviceUnavailable):
        manager.connect()

    manager.select("SYN-UNAUTHORIZED")
    with pytest.raises(DeviceUnavailable):
        manager.connect()


def test_simulated_adapter_has_no_adb_write_calls() -> None:
    adapter = FakeDeviceAdapter(
        devices=(DeviceInfo("SYN-DEVICE-A", state="device", authorized=True),)
    )
    manager = DeviceManager(adapter)

    manager.refresh()
    manager.select("SYN-DEVICE-A")
    manager.connect()
    manager.launch_app("com.ss.android.pda")

    assert adapter.adb_write_call_count == 0
    assert adapter.simulated_action_count >= 1


def test_refresh_disconnects_a_device_that_becomes_offline() -> None:
    adapter = FakeDeviceAdapter(
        devices=(DeviceInfo("SYN-DEVICE-A", state="device", authorized=True),)
    )
    manager = DeviceManager(adapter)
    manager.refresh()
    manager.connect()

    adapter.devices = (DeviceInfo("SYN-DEVICE-A", state="offline", authorized=False),)
    manager.refresh()

    assert manager.connected_device is None
    assert manager.selected_serial is None
    assert manager.connection_state.value == "disconnected"
    assert ("disconnect",) in adapter.actions
