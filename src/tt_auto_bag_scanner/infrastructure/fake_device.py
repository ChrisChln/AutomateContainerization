"""Offline adapter used by simulator tasks and fixture-driven tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping
from xml.etree import ElementTree as ET

from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceInfo,
    InputField,
    ScanOutcome,
    XmlSnapshot,
)


class FakeDeviceAdapter:
    """A deterministic UI fake; it never imports or invokes ADB."""

    is_simulated = True

    def __init__(
        self,
        *,
        devices: tuple[DeviceInfo, ...] = (),
        initial_xml: str | Path | None = None,
        snapshots_after_enter: Iterable[str | Path] = (),
        snapshots_after_back: Iterable[str | Path] = (),
        snapshots_after_tap: Iterable[str | Path] = (),
        package_name: str = "com.ss.android.pda",
        warehouse: str | None = None,
        carrier_configs: Iterable[CarrierConfig] = (),
        container_sort_map: Mapping[str, str] | None = None,
        outcome_map: Mapping[str, ScanOutcome | str] | None = None,
        initial_page: str | None = None,
    ) -> None:
        self.devices = tuple(devices)
        self.package_name = package_name
        self.actions: list[tuple[object, ...]] = []
        self.adb_write_call_count = 0
        self.simulated_action_count = 0
        self.session_generation = 0
        self._connected_serial: str | None = None
        self._current_xml = self._read(initial_xml) if initial_xml is not None else ""
        self._next_snapshots = [self._read(value) for value in snapshots_after_enter]
        self._next_back_snapshots = [self._read(value) for value in snapshots_after_back]
        self._next_tap_snapshots = [self._read(value) for value in snapshots_after_tap]
        self._entered_text: list[str] = []
        configs = tuple(carrier_configs)
        self._dynamic_mode = (
            warehouse is not None
            or container_sort_map is not None
            or bool(configs)
            or outcome_map is not None
        )
        self._dynamic_warehouse = warehouse.strip() if warehouse else ""
        self._container_sort_map = dict(container_sort_map or {})
        for config in configs:
            existing_sort = self._container_sort_map.get(config.container_no)
            if existing_sort is not None and existing_sort != config.sort_code:
                raise ValueError("carrier config conflicts with container sort mapping")
            self._container_sort_map[config.container_no] = config.sort_code
        if self._dynamic_mode and (not self._dynamic_warehouse or not self._container_sort_map):
            raise ValueError("dynamic mode requires a warehouse and container sort mapping")
        self._outcome_map = {
            tracking: self._normalize_outcome(outcome)
            for tracking, outcome in (outcome_map or {}).items()
        }
        if any(not tracking.strip() for tracking in self._outcome_map):
            raise ValueError("outcome map tracking keys must be non-empty")
        if initial_page is not None and initial_page not in {
            "home",
            "container_menu",
            "single_empty",
        }:
            raise ValueError("unsupported dynamic initial page")
        if initial_page is not None and not self._dynamic_mode:
            raise ValueError("initial_page requires dynamic mode")
        self._dynamic_page = initial_page or ("home" if self._dynamic_mode else "single_empty")
        self._dynamic_container = ""
        self._dynamic_sort_code = ""
        self._dynamic_input = ""
        self._dynamic_latest_tracking = ""
        self._dynamic_status = ""
        self._dynamic_error = ""
        self._dynamic_logid = ""
        self._dynamic_pending_duplicate = ""
        self._dynamic_scanned: dict[str, set[str]] = {}
        self._dynamic_logid_count = 0
        self._dynamic_back_once = False

    @staticmethod
    def _read(value: str | Path) -> str:
        if isinstance(value, Path):
            return value.read_text(encoding="utf-8")
        return value

    @staticmethod
    def _normalize_outcome(outcome: ScanOutcome | str) -> ScanOutcome:
        if isinstance(outcome, ScanOutcome):
            return outcome
        try:
            return ScanOutcome(str(outcome).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unsupported simulated outcome: {outcome}") from exc

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        self.actions.append(("list_devices",))
        return self.devices

    def connect(self, serial: str) -> DeviceInfo:
        self.actions.append(("connect", serial))
        info = next((device for device in self.devices if device.serial == serial), None)
        if info is None or info.state.lower() != "device" or not info.authorized:
            raise RuntimeError("fake device unavailable")
        self._connected_serial = serial
        self.session_generation += 1
        return info

    def disconnect(self) -> None:
        self.actions.append(("disconnect",))
        self._connected_serial = None
        self.session_generation += 1

    def launch_app(self, package_name: str) -> None:
        self.actions.append(("launch_app", package_name))
        self.simulated_action_count += 1
        self.session_generation += 1

    def snapshot_xml(self) -> XmlSnapshot:
        self.actions.append(("snapshot_xml",))
        return XmlSnapshot(
            xml=self._dynamic_xml() if self._dynamic_mode else self._current_xml,
            captured_at_utc=datetime.now(UTC),
            package_name=self.package_name,
            redacted_ref=(
                f"fake:dynamic/{self._dynamic_page}" if self._dynamic_mode else ""
            ),
        )

    def tap(self, x: int, y: int) -> None:
        self.actions.append(("tap", x, y))
        self.simulated_action_count += 1
        if self._dynamic_mode:
            self._dynamic_tap(x, y)
        elif self._next_tap_snapshots:
            self._current_xml = self._next_tap_snapshots.pop(0)

    def _dynamic_tap(self, x: int, y: int) -> None:
        try:
            root = ET.fromstring(self._dynamic_xml())
        except ET.ParseError:
            self._dynamic_page = "unknown"
            self._dynamic_error = "模拟界面 XML 无效"
            return
        for node in root.iter():
            if node.attrib.get("package") != self.package_name:
                continue
            if node.attrib.get("visible-to-user") != "true" or node.attrib.get("enabled") != "true":
                continue
            bounds = self._parse_bounds(node.attrib.get("bounds", ""))
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            if not (left <= x <= right and top <= y <= bottom):
                continue
            text = node.attrib.get("text", "").strip()
            if self._dynamic_page == "home" and text in {"组大包", "大包管理"}:
                self._dynamic_page = "container_menu"
                return
            if self._dynamic_page == "container_menu" and text == "单排序":
                self._dynamic_page = "single_empty"
                return

    @staticmethod
    def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value)
        if not match:
            return None
        bounds = tuple(int(part) for part in match.groups())
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return None
        return bounds

    def tap_field(self, field: InputField) -> None:
        if not field.editable:
            raise ValueError("cannot tap a non-editable field")
        left, top, right, bottom = field.bounds
        self.tap((left + right) // 2, (top + bottom) // 2)

    def enter_text(self, text: str) -> None:
        self.actions.append(("enter_text", text))
        self._entered_text.append(text)
        if self._dynamic_mode:
            self._dynamic_input = text
        self.simulated_action_count += 1

    def press_key(self, key: str) -> None:
        normalized = key.upper()
        self.actions.append(("press_key", normalized))
        self.simulated_action_count += 1
        if self._dynamic_mode:
            self._dynamic_press(normalized)
            return
        if normalized == "ENTER" and self._next_snapshots:
            self._current_xml = self._next_snapshots.pop(0)
        if normalized == "BACK" and self._next_back_snapshots:
            self._current_xml = self._next_back_snapshots.pop(0)

    def _dynamic_press(self, key: str) -> None:
        if key == "BACK":
            if self._dynamic_page in {"bag_ready", "scan_result"}:
                if not self._dynamic_back_once:
                    self._dynamic_back_once = True
                else:
                    self._dynamic_page = "single_empty"
                    self._dynamic_container = ""
                    self._dynamic_sort_code = ""
                    self._dynamic_latest_tracking = ""
                    self._dynamic_status = ""
                    self._dynamic_back_once = False
            return
        if key != "ENTER":
            return

        if self._dynamic_page == "single_empty":
            container = self._dynamic_input.strip()
            sort_code = self._container_sort_map.get(container)
            if not container or not sort_code:
                self._dynamic_page = "unknown"
                self._dynamic_error = "未配置的模拟容器"
                return
            self._dynamic_container = container
            self._dynamic_sort_code = sort_code
            self._dynamic_page = "bag_ready"
            self._dynamic_input = ""
            self._dynamic_latest_tracking = ""
            self._dynamic_status = ""
            self._dynamic_error = ""
            self._dynamic_logid = ""
            self._dynamic_back_once = False
            self._dynamic_scanned.setdefault(container, set())
            return

        if self._dynamic_page == "duplicate_dialog":
            tracking = self._dynamic_input.strip()
            if tracking and tracking == self._dynamic_pending_duplicate:
                self._dynamic_scanned.setdefault(self._dynamic_container, set()).add(tracking)
                self._dynamic_latest_tracking = tracking
                self._dynamic_page = "bag_ready"
                self._dynamic_pending_duplicate = ""
                self._dynamic_input = ""
                self._dynamic_status = ""
                self._dynamic_error = ""
                self._dynamic_logid = ""
            else:
                self._dynamic_page = "unknown"
                self._dynamic_error = "重复件恢复输入不匹配"
            return

        if self._dynamic_page not in {"bag_ready", "scan_result"}:
            self._dynamic_page = "unknown"
            return
        tracking = self._dynamic_input.strip()
        if not tracking:
            self._dynamic_page = "unknown"
            self._dynamic_error = "模拟输入为空"
            return
        if tracking in self._dynamic_scanned.setdefault(self._dynamic_container, set()):
            outcome = ScanOutcome.ALREADY_IN_BAG
        else:
            outcome = self._outcome_map.get(tracking, ScanOutcome.SUCCESS)
        self._dynamic_input = ""
        self._dynamic_latest_tracking = ""
        self._dynamic_error = ""
        self._dynamic_logid = ""
        if outcome is ScanOutcome.ALREADY_IN_BAG:
            self._dynamic_pending_duplicate = tracking
            self._dynamic_logid = self._next_dynamic_logid()
            self._dynamic_error = "已扫描包裹已在当前大包中"
            self._dynamic_page = "duplicate_dialog"
        elif outcome is ScanOutcome.SUCCESS:
            self._dynamic_scanned.setdefault(self._dynamic_container, set()).add(tracking)
            self._dynamic_latest_tracking = tracking
            self._dynamic_status = "扫描成功"
            self._dynamic_page = "scan_result"
        elif outcome is ScanOutcome.REJECTED:
            self._dynamic_latest_tracking = tracking
            self._dynamic_status = "扫描失败"
            self._dynamic_error = "模拟拒绝：该运单不可扫描"
            self._dynamic_logid = self._next_dynamic_logid()
            self._dynamic_page = "scan_result"
        else:
            self._dynamic_page = "unknown"
            self._dynamic_error = "模拟结果无法确认"

    def _next_dynamic_logid(self) -> str:
        self._dynamic_logid_count += 1
        return f"SYN-LOG-{self._dynamic_logid_count:06d}"

    def _dynamic_xml(self) -> str:
        root = ET.Element("hierarchy")

        def add(
            resource_id: str,
            text: str,
            *,
            class_name: str = "android.widget.TextView",
            bounds: str = "[0,0][360,48]",
            focused: bool = False,
            enabled: bool = True,
        ) -> None:
            attributes = {
                "package": self.package_name,
                "class": class_name,
                "text": text,
                "visible-to-user": "true",
                "enabled": str(enabled).lower(),
                "bounds": bounds,
                "resource-id": resource_id,
            }
            if class_name.endswith("EditText"):
                attributes["focused"] = str(focused).lower()
            ET.SubElement(root, "node", attributes)

        if self._dynamic_page == "unknown":
            add("unknown_state", self._dynamic_error or "模拟未知页面")
            return ET.tostring(root, encoding="unicode")

        if self._dynamic_page == "home":
            add("home_title", "工作台")
            add("warehouse_label", "仓库")
            add("warehouse_value", self._dynamic_warehouse)
            add(
                "group_bag_button",
                "组大包",
                class_name="android.widget.Button",
                bounds="[10,100][210,160]",
            )
            return ET.tostring(root, encoding="unicode")
        if self._dynamic_page == "container_menu":
            add("container_menu_title", "大包管理")
            add("warehouse_label", "仓库")
            add("warehouse_value", self._dynamic_warehouse)
            add(
                "single_sort_button",
                "单排序",
                class_name="android.widget.Button",
                bounds="[20,100][180,150]",
            )
            add(
                "mixed_sort_button",
                "混合排序",
                class_name="android.widget.Button",
                bounds="[220,100][380,150]",
            )
            return ET.tostring(root, encoding="unicode")

        add("page_title", "单件扫描")
        add("warehouse_label", "仓库")
        add("warehouse_value", self._dynamic_warehouse)
        if self._dynamic_page == "single_empty":
            add("bag_label", "大包号")
            add("bag_value", "")
            add("sort_label", "排序码")
            add("sort_value", "")
            add(
                "input_box",
                "",
                class_name="android.widget.EditText",
                bounds="[20,180][400,240]",
                focused=True,
            )
        elif self._dynamic_page == "duplicate_dialog":
            add("duplicate_error", self._dynamic_error)
            add("log_id", f"Logid: {self._dynamic_logid}")
            add(
                "disabled_input",
                "",
                class_name="android.widget.EditText",
                bounds="[10,220][300,270]",
                focused=False,
                enabled=False,
            )
            add(
                "retry_tracking",
                "",
                class_name="android.widget.EditText",
                bounds="[20,280][400,330]",
                focused=True,
            )
        else:
            add("bag_label", "大包号")
            add("bag_value", self._dynamic_container)
            add("sort_label", "排序码")
            add("sort_value", self._dynamic_sort_code)
            add(
                "input_box",
                "",
                class_name="android.widget.EditText",
                bounds="[20,180][400,240]",
                focused=True,
            )
            if self._dynamic_latest_tracking:
                add("recent_scan_header", "最近扫描")
                add("recent_scan_item", self._dynamic_latest_tracking)
            if self._dynamic_status == "扫描成功":
                add("success_status", self._dynamic_status)
            elif self._dynamic_status == "扫描失败":
                add("rejected_status", self._dynamic_status)
                add("rejected_reason", self._dynamic_error)
                add("log_id", f"Logid: {self._dynamic_logid}")
        return ET.tostring(root, encoding="unicode")

    def metadata(self) -> DeviceInfo:
        if self._connected_serial:
            return next(
                device for device in self.devices if device.serial == self._connected_serial
            )
        if len(self.devices) == 1:
            return self.devices[0]
        return DeviceInfo(
            serial="simulator",
            model="simulator",
            state="device",
            authorized=True,
        )

    @property
    def entered_text(self) -> tuple[str, ...]:
        return tuple(self._entered_text)

