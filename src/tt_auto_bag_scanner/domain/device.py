"""Ports implemented by the simulated and ADB device adapters."""

from __future__ import annotations

from typing import Protocol

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    InputField,
    PageObservation,
    PhysicalCheck,
    QueueItem,
    RecoveryResult,
    ScanResult,
    VerifiedSession,
    XmlSnapshot,
)


class DeviceAdapter(Protocol):
    is_simulated: bool

    def list_devices(self) -> tuple[DeviceInfo, ...]: ...

    def connect(self, serial: str) -> DeviceInfo: ...

    def disconnect(self) -> None: ...

    def launch_app(self, package_name: str) -> None: ...

    def snapshot_xml(self) -> XmlSnapshot: ...

    def tap(self, x: int, y: int) -> None: ...

    def tap_field(self, field: InputField) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None: ...

    def enter_text(self, text: str) -> None: ...

    def clear_text(self) -> None: ...

    def press_key(self, key: str) -> None: ...

    def metadata(self) -> DeviceInfo: ...


class WmsController(Protocol):
    @property
    def device_mode(self) -> DeviceMode | None: ...

    def open_bag(self, config: CarrierConfig) -> VerifiedSession: ...

    def verify_current_or_open_bag(self, config: CarrierConfig) -> VerifiedSession: ...

    def submit_once(
        self,
        item: QueueItem,
        session: VerifiedSession,
        physical_check: PhysicalCheck,
        attempt_id: str,
    ) -> ScanResult: ...

    def recover_duplicate(self, result: ScanResult, session: VerifiedSession) -> RecoveryResult: ...

    def observe(self) -> PageObservation: ...
