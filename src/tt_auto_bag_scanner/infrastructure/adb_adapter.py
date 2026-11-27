"""Explicitly constructed uiautomator2 adapter for a site-authorized device."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tt_auto_bag_scanner.domain.models import DeviceInfo, InputField, XmlSnapshot
from tt_auto_bag_scanner.infrastructure.exception_handler import DeviceActionError


class AdbDeviceAdapter:
    """ADB access is lazy; constructing this class makes no device connection."""

    is_simulated = False

    def __init__(
        self,
        *,
        serial: str | None = None,
        package_name: str = "com.ss.android.pda",
        adb_path: str | Path = "adb",
        uiautomator_client: Any | None = None,
    ) -> None:
        if not package_name.strip():
            raise ValueError("WMS package name is required")
        self.serial = serial
        self.package_name = package_name
        self.adb_path = str(adb_path)
        self._device: Any | None = uiautomator_client
        self._authorized = False
        self.session_generation = 0

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        try:
            result = subprocess.run(
                [self.adb_path, "devices", "-l"],
                capture_output=True,
                check=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeviceActionError("ADB device listing failed") from exc
        devices = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.strip().split()
            if not fields:
                continue
            serial = fields[0]
            state = fields[1] if len(fields) > 1 else "unknown"
            model = next(
                (part.split(":", 1)[1] for part in fields if part.startswith("model:")),
                "",
            )
            devices.append(
                DeviceInfo(
                    serial=serial,
                    model=model,
                    state=state,
                    authorized=state == "device",
                )
            )
        return tuple(devices)

    def connect(self, serial: str) -> DeviceInfo:
        if not serial.strip():
            raise ValueError("explicit device serial is required")
        self._device = None
        self._authorized = False
        self.session_generation += 1
        listed = next((device for device in self.list_devices() if device.serial == serial), None)
        if listed is None or listed.state != "device" or not listed.authorized:
            raise DeviceActionError("selected PDA is offline or unauthorized")
        try:
            import uiautomator2 as u2

            self._device = u2.connect(serial)
            info = self._device.device_info
        except Exception as exc:
            self._device = None
            self._authorized = False
            raise DeviceActionError("unable to connect to authorized device") from exc
        app_version = ""
        try:
            app_info = self._device.app_info(self.package_name)
            version_name = str(app_info.get("versionName", "") or "").strip()
            version_code = str(app_info.get("versionCode", "") or "").strip()
            if version_name and version_code:
                app_version = f"{version_name} ({version_code})"
            else:
                app_version = version_name or version_code
        except Exception:
            pass
        self.serial = serial
        self._authorized = True
        return DeviceInfo(
            serial=serial,
            model=str(info.get("model", "")),
            manufacturer=str(info.get("brand", info.get("manufacturer", ""))),
            android_version=str(info.get("version", "")),
            sdk_version=str(info.get("sdk", "")),
            state="device",
            authorized=True,
            app_version=app_version,
        )

    def disconnect(self) -> None:
        self._device = None
        self._authorized = False
        self.session_generation += 1

    def launch_app(self, package_name: str) -> None:
        device = self._require_device()
        if package_name != self.package_name:
            raise ValueError("package does not match configured WMS package")
        try:
            device.app_start(package_name, stop=False)
        except Exception as exc:
            raise DeviceActionError("WMS app launch failed") from exc
        self.session_generation += 1

    def snapshot_xml(self) -> XmlSnapshot:
        device = self._require_device()
        try:
            xml = device.dump_hierarchy(compressed=False)
            current = device.app_current()
        except Exception as exc:
            raise DeviceActionError("unable to read WMS page hierarchy") from exc
        package = str(current.get("package", ""))
        activity = str(current.get("activity", ""))
        return XmlSnapshot(
            xml=xml,
            captured_at_utc=datetime.now(UTC),
            package_name=package,
            activity_name=activity,
        )

    def tap(self, x: int, y: int) -> None:
        try:
            self._require_device().click(x, y)
        except Exception as exc:
            raise DeviceActionError("device tap failed") from exc

    def tap_field(self, field: InputField) -> None:
        if not field.editable:
            raise ValueError("cannot tap a non-editable field")
        left, top, right, bottom = field.bounds
        self.tap((left + right) // 2, (top + bottom) // 2)

    def enter_text(self, text: str) -> None:
        try:
            self._require_device().send_keys(text, clear=True)
        except Exception as exc:
            raise DeviceActionError("device text entry failed") from exc

    def press_key(self, key: str) -> None:
        key_map = {"ENTER": "enter", "BACK": "back"}
        normalized = key.upper()
        if normalized not in key_map:
            raise ValueError("unsupported device key")
        try:
            self._require_device().press(key_map[normalized])
        except Exception as exc:
            raise DeviceActionError("device key press failed") from exc

    def metadata(self) -> DeviceInfo:
        if not self._authorized:
            return DeviceInfo(
                serial=self.serial or "",
                state="unauthorized",
                authorized=False,
            )
        device = self._require_device()
        try:
            info = device.device_info
        except Exception as exc:
            raise DeviceActionError("device metadata is unavailable") from exc
        return DeviceInfo(
            serial=self.serial or "",
            model=str(info.get("model", "")),
            manufacturer=str(info.get("brand", info.get("manufacturer", ""))),
            android_version=str(info.get("version", "")),
            sdk_version=str(info.get("sdk", "")),
            state="device",
            authorized=True,
        )

    def _require_device(self) -> Any:
        if self._device is None or not self._authorized:
            raise DeviceActionError("no verified authorized PDA connection is active")
        return self._device

