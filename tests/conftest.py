from __future__ import annotations

import pytest
from PySide6.QtCore import QProcess

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter


@pytest.fixture(autouse=True)
def _never_reach_a_real_pda(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep both the ADB adapter and scrcpy inert in automated tests."""
    monkeypatch.setattr(QProcess, "start", lambda self, *args: None)
    monkeypatch.setattr(
        AppController,
        "_new_adb_adapter",
        lambda self, adb_path: FakeDeviceAdapter(),
    )


class FakeRealPda:
    """Test-only PDA double that goes through the real-device guards."""

    serial = "SYNTHETIC-PDA"
    warehouse = "WAREHOUSE-SYNTHETIC"

    def __init__(self, container_sort_map: dict[str, str], **options: object) -> None:
        from tt_auto_bag_scanner.domain.models import DeviceInfo

        self.adapter = FakeDeviceAdapter(
            devices=(DeviceInfo(serial=self.serial, state="device", authorized=True),),
            warehouse=self.warehouse,
            container_sort_map=container_sort_map,
            **options,
        )
        self.adapter.is_simulated = False

    def factory(self, adb_path: str) -> FakeDeviceAdapter:
        return self.adapter

    def authorize_and_connect(self, app, controller: AppController) -> None:
        import time

        controller.settings["warehouse"] = self.warehouse
        controller.settings.setdefault("operator_id", "operator-test")
        controller.real_mode_authorized = True
        controller.device_manager.refresh()
        controller._connect_device(self.serial)
        deadline = time.monotonic() + 5
        while not controller._connected and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert controller._connected, "fake PDA did not connect"


@pytest.fixture
def fake_real_pda():
    return FakeRealPda
