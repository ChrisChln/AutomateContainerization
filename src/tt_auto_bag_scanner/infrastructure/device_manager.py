"""Selected-device lifecycle and fail-closed connection checks."""

from __future__ import annotations

from enum import StrEnum

from tt_auto_bag_scanner.domain.device import DeviceAdapter
from tt_auto_bag_scanner.domain.models import DeviceInfo


class DeviceError(RuntimeError):
    """Base class for device selection and connection errors."""


class DeviceSelectionRequired(DeviceError):
    """Raised when more than one eligible device needs explicit selection."""


class DeviceUnavailable(DeviceError):
    """Raised when the selected device is absent, offline, or unauthorized."""


class DeviceConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


class DeviceManager:
    """Owns one selected adapter connection; never chooses among multiple devices."""

    def __init__(self, adapter: DeviceAdapter) -> None:
        self.adapter = adapter
        self.devices: tuple[DeviceInfo, ...] = ()
        self.selected_serial: str | None = None
        self.connection_state = DeviceConnectionState.DISCONNECTED
        self.connected_device: DeviceInfo | None = None

    def refresh(self) -> tuple[DeviceInfo, ...]:
        self.devices = tuple(self.adapter.list_devices())
        if self.selected_serial:
            selected = next(
                (device for device in self.devices if device.serial == self.selected_serial), None
            )
            if selected is None or selected.state.lower() != "device" or not selected.authorized:
                if self.connection_state is DeviceConnectionState.CONNECTED:
                    self.adapter.disconnect()
                self.selected_serial = None
                self.connection_state = DeviceConnectionState.DISCONNECTED
                self.connected_device = None
        return self.devices

    def select(self, serial: str) -> DeviceInfo:
        if self.connection_state is DeviceConnectionState.CONNECTED:
            self.disconnect()
        for device in self.devices:
            if device.serial == serial:
                self.selected_serial = serial
                return device
        raise DeviceUnavailable("selected device is not in the current device list")

    def connect(self, serial: str | None = None) -> DeviceInfo:
        if serial is not None:
            self.select(serial)
        if self.selected_serial is None:
            if not self.devices:
                raise DeviceUnavailable("no device is available")
            if len(self.devices) > 1:
                raise DeviceSelectionRequired("select a device before connecting")
            self.selected_serial = self.devices[0].serial

        candidate = next(
            (device for device in self.devices if device.serial == self.selected_serial), None
        )
        if candidate is None:
            raise DeviceUnavailable("selected device is no longer available")
        if candidate.state.lower() != "device" or not candidate.authorized:
            raise DeviceUnavailable(f"device is {candidate.state}; authorization is required")

        try:
            connected = self.adapter.connect(self.selected_serial)
        except Exception as exc:
            self.connection_state = DeviceConnectionState.DISCONNECTED
            self.connected_device = None
            raise DeviceUnavailable("device connection failed") from exc
        if connected.serial != self.selected_serial:
            self.adapter.disconnect()
            raise DeviceUnavailable("connected device does not match selected device")
        self.connected_device = connected
        self.connection_state = DeviceConnectionState.CONNECTED
        return connected

    def disconnect(self) -> None:
        if self.connection_state is DeviceConnectionState.CONNECTED:
            self.adapter.disconnect()
        self.connected_device = None
        self.connection_state = DeviceConnectionState.DISCONNECTED

    def launch_app(self, package_name: str) -> None:
        if self.connection_state is not DeviceConnectionState.CONNECTED:
            raise DeviceUnavailable("connect a device before launching the app")
        if not package_name.strip():
            raise ValueError("WMS package name is required")
        self.adapter.launch_app(package_name)

