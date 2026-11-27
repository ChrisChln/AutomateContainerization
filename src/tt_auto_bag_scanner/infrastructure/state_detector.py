"""Strict, read-only parsing of visible WMS UI XML."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from tt_auto_bag_scanner.domain.models import (
    InputField,
    PageObservation,
    PageState,
    XmlSnapshot,
)

_DUPLICATE_ERROR = "已扫描包裹已在当前大包中"
_LOGID = re.compile(r"\bLogid\s*[:：#]?\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
_RECENT_RESOURCE = re.compile(r"recent.*(?:scan|record)|(?:scan|record).*recent", re.IGNORECASE)


class TapTargetError(ValueError):
    """An exact visible target could not be identified uniquely and safely."""


@dataclass(frozen=True, slots=True)
class TapTarget:
    text: str
    resource_id: str
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


def _true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _bounds(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value.strip())
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _visible(element: ET.Element) -> bool:
    value = element.attrib.get("visible-to-user")
    return _true(value)


def _enabled(element: ET.Element) -> bool:
    value = element.attrib.get("enabled")
    return value is None or _true(value)


class StateDetector:
    def __init__(self, *, target_package: str = "com.ss.android.pda") -> None:
        if not target_package.strip():
            raise ValueError("target package is required")
        self.target_package = target_package

    def detect(self, snapshot: XmlSnapshot) -> PageObservation:
        if snapshot.package_name != self.target_package:
            return PageObservation(state=PageState.UNKNOWN, redacted_xml_ref=snapshot.redacted_ref)
        try:
            root = ET.fromstring(snapshot.xml)
        except (ET.ParseError, TypeError):
            return PageObservation(state=PageState.UNKNOWN, redacted_xml_ref=snapshot.redacted_ref)

        entries: list[tuple[ET.Element, str, str, str]] = []
        for node in root.iter():
            if not _visible(node):
                continue
            text = node.attrib.get("text", "").strip()
            resource_id = node.attrib.get("resource-id", "")
            class_name = node.attrib.get("class", "")
            if text or class_name.endswith(("EditText", "TextView", "Button")):
                if node.attrib.get("package") != self.target_package:
                    return PageObservation(
                        state=PageState.UNKNOWN,
                        redacted_xml_ref=snapshot.redacted_ref,
                    )
                entries.append((node, text, resource_id, class_name))

        labels = self._labels(entries, root)
        fields = self._focused_inputs(entries)
        enabled_editors = tuple(
            node
            for node, _, _, class_name in entries
            if class_name.endswith("EditText") and _enabled(node)
        )
        text_values = [text for _, text, _, _ in entries if text]
        all_text = "\n".join(text_values)
        dialog_text = self._dialog_text(entries)
        logid = self._extract_logid(all_text)
        latest = self._recent_tracking(entries, root)
        state = self._classify(
            entries,
            all_text,
            labels,
            fields,
            latest,
            logid,
            enabled_editor_count=len(enabled_editors),
        )

        return PageObservation(
            state=state,
            visible_labels=labels,
            focused_inputs=fields,
            latest_tracking_records=latest,
            dialog_text=dialog_text,
            logid=logid,
            redacted_xml_ref=snapshot.redacted_ref,
        )

    def find_unique_target(
        self,
        snapshot: XmlSnapshot,
        *,
        expected_state: PageState,
        exact_texts: tuple[str, ...],
        resource_ids: tuple[str, ...] = (),
    ) -> TapTarget:
        """Resolve one fresh, visible, enabled target in the expected app/page."""
        observation = self.detect(snapshot)
        if observation.state is not expected_state:
            raise TapTargetError("page changed before target selection")
        if snapshot.package_name != self.target_package:
            raise TapTargetError("target package cannot be verified")
        try:
            root = ET.fromstring(snapshot.xml)
        except (ET.ParseError, TypeError) as exc:
            raise TapTargetError("current hierarchy is invalid") from exc

        exact_labels = {label.strip() for label in exact_texts if label.strip()}
        exact_ids = set(resource_ids)
        matches: list[TapTarget] = []
        for node in root.iter():
            if node.attrib.get("package") != self.target_package:
                continue
            if not _true(node.attrib.get("visible-to-user")):
                continue
            if not _true(node.attrib.get("enabled")):
                continue
            bounds = _bounds(node.attrib.get("bounds"))
            if bounds is None or bounds[0] < 0 or bounds[1] < 0:
                continue
            text = node.attrib.get("text", "").strip()
            resource_id = node.attrib.get("resource-id", "")
            if text not in exact_labels and resource_id not in exact_ids:
                continue
            matches.append(TapTarget(text=text, resource_id=resource_id, bounds=bounds))

        if len(matches) != 1:
            raise TapTargetError("expected one exact visible target")
        return matches[0]

    def _labels(
        self, entries: list[tuple[ET.Element, str, str, str]], root: ET.Element
    ) -> dict[str, str]:
        labels: dict[str, str] = {}
        for _, text, resource_id, _ in entries:
            key = resource_id.rsplit("/", 1)[-1].lower()
            if key in {"bag_value", "container_value", "actual_bag", "actual_container"}:
                labels["大包号"] = text
            elif key in {"sort_value", "actual_sort", "sort_code"}:
                labels["排序码"] = text
            elif key in {"warehouse_value", "actual_warehouse"}:
                labels["仓库"] = text
            elif key in {"success_status", "scan_status", "result_status", "rejected_status"}:
                labels["扫描状态"] = text
            elif text:
                labels.setdefault(text, text)

        for parent in root.iter():
            children = list(parent)
            for index, child in enumerate(children[:-1]):
                if not _visible(child):
                    continue
                text = child.attrib.get("text", "").strip()
                next_text = ""
                for sibling in children[index + 1 :]:
                    if _visible(sibling):
                        next_text = sibling.attrib.get("text", "").strip()
                        if next_text or sibling.attrib.get("resource-id"):
                            break
                if not next_text:
                    continue
                if text in {"大包号", "大包", "Container", "Bag"}:
                    labels.setdefault("大包号", next_text)
                elif text in {"排序码", "分拣码", "Sort code", "Sort"}:
                    labels.setdefault("排序码", next_text)
                elif text in {"仓库", "Warehouse"}:
                    labels.setdefault("仓库", next_text)
        return labels

    def _focused_inputs(
        self, entries: list[tuple[ET.Element, str, str, str]]
    ) -> tuple[InputField, ...]:
        fields: list[InputField] = []
        for node, text, resource_id, class_name in entries:
            if not class_name.endswith("EditText") or not _enabled(node):
                continue
            bounds = _bounds(node.attrib.get("bounds"))
            if bounds is None or not _true(node.attrib.get("focused")):
                continue
            fields.append(
                InputField(
                    resource_id=resource_id,
                    text=text,
                    class_name=class_name,
                    bounds=bounds,
                    focused=True,
                    editable=True,
                )
            )
        return tuple(fields)

    @staticmethod
    def _dialog_text(entries: list[tuple[ET.Element, str, str, str]]) -> str:
        exact_duplicate = next(
            (
                text
                for _, text, _, _ in entries
                if text == _DUPLICATE_ERROR
            ),
            "",
        )
        if exact_duplicate:
            return exact_duplicate
        for _, text, resource_id, _ in entries:
            if any(token in resource_id.lower() for token in ("dialog", "error", "reason")):
                if text:
                    return text
        return ""

    @staticmethod
    def _extract_logid(text: str) -> str | None:
        match = _LOGID.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _recent_tracking(
        entries: list[tuple[ET.Element, str, str, str]], root: ET.Element
    ) -> tuple[str, ...]:
        records = []
        parent_map = {child: parent for parent in root.iter() for child in parent}
        for node, text, resource_id, class_name in entries:
            if (
                not text
                or class_name.endswith("EditText")
                or not class_name.endswith("TextView")
            ):
                continue
            if any(
                token in text.lower()
                for token in ("error", "失败", "已扫描包裹已在当前大包中")
            ):
                continue
            normalized_id = resource_id.rsplit("/", 1)[-1]
            is_result_id = normalized_id == "recent_scan_item" or (
                _RECENT_RESOURCE.search(resource_id)
                and re.search(r"(?:item|row|record)", normalized_id, re.IGNORECASE)
            )
            ancestor = parent_map.get(node)
            in_recent_area = False
            while ancestor is not None:
                ancestor_id = ancestor.attrib.get("resource-id", "")
                if _RECENT_RESOURCE.search(ancestor_id):
                    in_recent_area = True
                    break
                ancestor = parent_map.get(ancestor)
            if is_result_id or in_recent_area:
                records.append(text)
        return tuple(records)

    @staticmethod
    def _classify(
        entries: list[tuple[ET.Element, str, str, str]],
        all_text: str,
        labels: dict[str, str],
        fields: tuple[InputField, ...],
        latest: tuple[str, ...],
        logid: str | None,
        *,
        enabled_editor_count: int,
    ) -> PageState:
        if _DUPLICATE_ERROR in all_text and logid:
            return PageState.DUPLICATE_DIALOG
        ids = " ".join(resource_id.lower() for _, _, resource_id, _ in entries)
        lowered = all_text.lower()
        if (
            "login" in ids
            or "password" in ids
            or any(text in {"登录", "Login", "登录页"} for _, text, _, _ in entries)
        ):
            return PageState.LOGIN
        has_container_menu_title = any(
            text in all_text for text in ("大包管理", "容器管理", "Container menu")
        ) or "container_menu" in ids
        has_single_or_mixed_sort = any(
            text in all_text for text in ("单排序", "混合排序", "Single sort", "Mixed sort")
        )
        if has_container_menu_title and has_single_or_mixed_sort:
            return PageState.CONTAINER_MENU
        home_marker = any(
            text in all_text or marker in ids
            for text, marker in (
                ("工作台", "home_title"),
                ("首页", "home_title"),
                ("Home", "home_title"),
            )
        )
        home_action = any(
            text == "组大包"
            or (
                text == "大包管理"
                and (node.attrib.get("clickable") == "true" or "home" in resource_id.lower())
                and "container_menu" not in resource_id.lower()
            )
            for node, text, resource_id, _ in entries
        )
        if home_marker or home_action:
            return PageState.HOME
        if "单件扫描" in all_text or "single scan" in lowered:
            bag = labels.get("大包号", "").strip()
            sort_code = labels.get("排序码", "").strip()
            if bag and sort_code and enabled_editor_count == 1 and len(fields) == 1:
                success = any(token in all_text for token in ("扫描成功", "Scan successful"))
                if latest and success:
                    return PageState.SCAN_RESULT
                if labels.get("扫描状态") in {"扫描失败", "Scan rejected"} and logid:
                    return PageState.SCAN_RESULT
                return PageState.BAG_READY
            if (
                not bag
                and not sort_code
                and enabled_editor_count == 1
                and len(fields) == 1
                and not fields[0].text
            ):
                return PageState.SINGLE_EMPTY
        return PageState.UNKNOWN

