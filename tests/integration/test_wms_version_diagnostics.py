from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from tt_auto_bag_scanner.application.app_controller import AppController
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.wms_controller import WmsController
from tt_auto_bag_scanner.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    yield instance
    for widget in instance.topLevelWidgets():
        if widget.parent() is None:
            widget.close()
            widget.deleteLater()
    instance.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    instance.processEvents()


def test_container_verification_keeps_detected_wms_version(app: QApplication, tmp_path) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    controller._device_connected(
        DeviceInfo(serial="SYNTHETIC-DEVICE", app_version="1.1.23 (1001007)")
    )
    config = CarrierConfig(
        carrier_key="CARRIER-SYNTHETIC",
        display_name="Synthetic Carrier",
        aliases=(),
        sort_code="SORT-SYNTHETIC",
        container_no="CONTAINER-SYNTHETIC",
        enabled=True,
        version=1,
        updated_at_utc=datetime.now(UTC),
    )
    session = VerifiedSession(
        serial="SYNTHETIC-DEVICE",
        warehouse="WAREHOUSE-SYNTHETIC",
        container_no=config.container_no,
        sort_code=config.sort_code,
        session_id="SESSION-SYNTHETIC",
        verified_at_utc=datetime.now(UTC),
    )
    adapter = controller.adapter
    wms_controller = WmsController(adapter, warehouse=session.warehouse)
    controller.wms_controller = wms_controller
    controller._container_verified(
        wms_controller,
        session,
        config,
        expected_mode=DeviceMode.REAL,
        expected_adapter=adapter,
    )

    assert window.app_version_label.text() == "1.1.23 (1001007)"
    controller._run_diagnostics()
    assert window.diagnostic_labels["wms_version"].text() == "1.1.23 (1001007)"

    controller.close()
    window.close()


def test_diagnostics_distinguish_libusb_setting_from_version_and_session(
    app: QApplication, tmp_path, monkeypatch
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    controller.mode = DeviceMode.REAL
    monkeypatch.setenv("ADB_LIBUSB", "0")

    controller._run_diagnostics()

    assert window.diagnostic_labels["driver"].text() == "待检查"
    assert window.diagnostic_labels["wms"].text() == "未验证"
    assert window.diagnostic_labels["wms_version"].text() == "未读取"
    monkeypatch.setenv("ADB_LIBUSB", "1")
    controller._run_diagnostics()
    assert window.diagnostic_labels["driver"].text() == "环境已配置"

    controller.close()
    window.close()


def test_device_disconnect_clears_cached_wms_version_and_authorization(
    app: QApplication, tmp_path
) -> None:
    window = MainWindow()
    controller = AppController(window, data_dir=tmp_path)
    controller.mode = DeviceMode.REAL
    controller._wms_app_version = "1.1.23 (1001007)"
    controller._connected = True
    controller.real_mode_authorized = True

    controller._devices_refreshed(())
    controller._run_diagnostics()

    assert window.diagnostic_labels["wms_version"].text() == "未读取"
    assert controller.real_mode_authorized is False

    controller.close()
    window.close()
