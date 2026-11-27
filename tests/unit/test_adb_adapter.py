from __future__ import annotations

import sys
from types import SimpleNamespace

from tt_auto_bag_scanner.infrastructure.adb_adapter import AdbDeviceAdapter


class FakeUiautomatorDevice:
    device_info = {
        "model": "SYNTHETIC-PDA",
        "brand": "SYNTHETIC",
        "version": "11",
        "sdk": "30",
    }

    def __init__(self, app_info: object) -> None:
        self._app_info = app_info
        self.requested_package: str | None = None

    def app_info(self, package_name: str) -> object:
        self.requested_package = package_name
        if isinstance(self._app_info, Exception):
            raise self._app_info
        return self._app_info


def _connected_adapter(monkeypatch, device: FakeUiautomatorDevice) -> AdbDeviceAdapter:
    adapter = AdbDeviceAdapter(
        serial="SYN-DEVICE",
        package_name="com.example.wms",
        uiautomator_client=device,
    )
    adapter.list_devices = lambda: (  # type: ignore[method-assign]
        SimpleNamespace(serial="SYN-DEVICE", state="device", authorized=True),
    )
    monkeypatch.setitem(
        sys.modules,
        "uiautomator2",
        SimpleNamespace(connect=lambda _serial: device),
    )
    return adapter


def test_connect_reads_configured_wms_version_without_mutating_device(monkeypatch) -> None:
    device = FakeUiautomatorDevice({"versionName": "2.4.1", "versionCode": 20401})
    adapter = _connected_adapter(monkeypatch, device)

    info = adapter.connect("SYN-DEVICE")

    assert device.requested_package == "com.example.wms"
    assert info.app_version == "2.4.1 (20401)"
    assert info.authorized is True


def test_connect_succeeds_when_wms_version_lookup_is_unavailable(monkeypatch) -> None:
    device = FakeUiautomatorDevice(RuntimeError("version metadata unavailable"))
    adapter = _connected_adapter(monkeypatch, device)

    info = adapter.connect("SYN-DEVICE")

    assert info.app_version == ""
    assert info.authorized is True


def test_connect_succeeds_when_uiautomator_client_has_no_app_info(monkeypatch) -> None:
    class DeviceWithoutAppInfo:
        device_info = FakeUiautomatorDevice.device_info

    device = DeviceWithoutAppInfo()
    adapter = _connected_adapter(monkeypatch, device)  # type: ignore[arg-type]

    info = adapter.connect("SYN-DEVICE")

    assert info.app_version == ""
    assert info.authorized is True
