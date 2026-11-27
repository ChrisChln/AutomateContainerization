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

_SYSTEM_UI_PACKAGE = "com.android.systemui"
_DUPLICATE_ERROR = "已扫描包裹已在当前大包中"
_LOGID = re.compile(r"\bLogid\s*[:：#=]?\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
# Exact verified duplicate dialog body (real WMS 单排序): optional full stop, a
# non-empty Logid, then the fixed instruction ending with the submitted tracking.
_DUPLICATE_BODY = re.compile(
    r"已扫描包裹已在当前大包中。?;\s*Logid=([A-Za-z0-9_-]+)[,，]"
    r"请重新扫描追踪号码，然后扫描正确的容器\s+(\S+)"
)
_DIALOG_TITLE = "提示"
_SINGLE_SORT_TITLES = ("单件扫描", "单排序")
_LATEST_ANCHOR = "最后扫描的"
_ALL_RECORDS_LINK = "查看所有记录"
_LABEL_WORDS = {
    "大包号": "大包号",
    "大包": "大包号",
    "Container": "大包号",
    "Bag": "大包号",
    "排序码": "排序码",
    "分拣码": "排序码",
    "Sort code": "排序码",
    "Sort": "排序码",
    "仓库": "仓库",
    "Warehouse": "仓库",
    "总包裹数量": "总包裹数量",
    "扫描数量": "扫描数量",
}
_ERROR_TOKENS = ("error", "失败", "错误", "异常", "logid", _DUPLICATE_ERROR)
_RECENT_RESOURCE = re.compile(r"recent.*(?:scan|record)|(?:scan|record).*recent", re.IGNORECASE)

Entry = tuple[ET.Element, str, str, str]


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


def _text(element: ET.Element) -> str:
    return element.attrib.get("text", "").strip()


def _is_text_view(element: ET.Element) -> bool:
    return element.attrib.get("class", "").endswith("TextView")


def _looks_like_error(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in _ERROR_TOKENS)


class _Page:
    """Parsed visible hierarchy with parent links and an optional dialog subtree."""

    def __init__(self, root: ET.Element, entries: list[Entry]) -> None:
        self.root = root
        self.entries = entries
        self.parents = {child: parent for parent in root.iter() for child in parent}
        titles = [
            node
            for node, text, _, class_name in entries
            if text == _DIALOG_TITLE and class_name.endswith("TextView")
        ]
        self.dialog_ambiguous = len(titles) > 1
        self.dialog_root = self.parents.get(titles[0]) if len(titles) == 1 else None

    def in_dialog(self, node: ET.Element) -> bool:
        if self.dialog_root is None:
            return False
        current = self.parents.get(node)
        while current is not None:
            if current is self.dialog_root:
                return True
            current = self.parents.get(current)
        return False


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

        entries: list[Entry] = []
        for node in root.iter():
            if not _visible(node):
                continue
            if node.attrib.get("package") == _SYSTEM_UI_PACKAGE:
                continue
            text = _text(node)
            resource_id = node.attrib.get("resource-id", "")
            class_name = node.attrib.get("class", "")
            if text or class_name.endswith(("EditText", "TextView", "Button")):
                if node.attrib.get("package") != self.target_package:
                    return PageObservation(
                        state=PageState.UNKNOWN,
                        redacted_xml_ref=snapshot.redacted_ref,
                    )
                entries.append((node, text, resource_id, class_name))

        page = _Page(root, entries)
        labels = self._labels(page)
        input_nodes = self._inputs(page)
        inputs = tuple(field for _, field in input_nodes)
        fields = tuple(field for field in inputs if field.focused)
        dialog_inputs = tuple(field for node, field in input_nodes if page.in_dialog(node))
        all_text = "\n".join(text for _, text, _, _ in entries if text)
        logid = self._extract_logid(entries)
        latest, anchored = self._recent_tracking(page)
        duplicate_tracking = self._duplicate_tracking(page, logid)
        dialog_text = self._dialog_text(page)
        state = self._classify(
            page,
            all_text,
            labels,
            inputs,
            latest,
            anchored,
            logid,
            duplicate=bool(duplicate_tracking),
            has_error=bool(dialog_text),
        )

        return PageObservation(
            state=state,
            visible_labels=labels,
            focused_inputs=fields,
            latest_tracking_records=latest,
            dialog_text=dialog_text,
            logid=logid,
            redacted_xml_ref=snapshot.redacted_ref,
            input_fields=inputs,
            dialog_inputs=dialog_inputs,
            dialog_tracking=duplicate_tracking,
            duplicate_in_bag=bool(duplicate_tracking) and state is PageState.DUPLICATE_DIALOG,
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

    def _labels(self, page: _Page) -> dict[str, str]:
        labels: dict[str, str] = {}
        for node, text, resource_id, _ in page.entries:
            key = resource_id.rsplit("/", 1)[-1].lower()
            if key in {"bag_value", "container_value", "actual_bag", "actual_container"}:
                labels["大包号"] = text
            elif key in {"sort_value", "actual_sort", "sort_code"}:
                labels["排序码"] = text
            elif key in {"warehouse_value", "actual_warehouse", "wms_house_name_text"}:
                labels["仓库"] = text
            elif key in {"success_status", "scan_status", "result_status", "rejected_status"}:
                labels["扫描状态"] = text
            elif text and text not in _LABEL_WORDS:
                labels.setdefault(text, text)

        for node, text, _, _ in page.entries:
            label = _LABEL_WORDS.get(text)
            if label is None or label in labels or page.in_dialog(node):
                continue
            value = self._value_after_label(page, node)
            if value:
                labels[label] = value
        return labels

    @staticmethod
    def _value_after_label(page: _Page, node: ET.Element) -> str:
        """Value is the next visible text sibling of the label or its sole-child wrappers."""
        current = node
        for _ in range(3):
            parent = page.parents.get(current)
            if parent is None:
                return ""
            siblings = [child for child in parent if _visible(child)]
            if current not in siblings:
                return ""
            for sibling in siblings[siblings.index(current) + 1 :]:
                value = _text(sibling)
                if value:
                    return value if _is_text_view(sibling) else ""
                if sibling.attrib.get("resource-id"):
                    return ""
            if len(siblings) != 1:
                return ""
            current = parent
        return ""

    @staticmethod
    def _inputs(page: _Page) -> list[tuple[ET.Element, InputField]]:
        fields: list[tuple[ET.Element, InputField]] = []
        for node, text, resource_id, class_name in page.entries:
            if not class_name.endswith("EditText") or not _enabled(node):
                continue
            bounds = _bounds(node.attrib.get("bounds"))
            if bounds is None:
                continue
            field = InputField(
                resource_id=resource_id,
                text=text,
                class_name=class_name,
                bounds=bounds,
                focused=_true(node.attrib.get("focused")),
                editable=True,
            )
            fields.append((node, field))
        return fields

    @staticmethod
    def _dialog_text(page: _Page) -> str:
        if page.dialog_root is not None:
            for node, text, _, _ in page.entries:
                if text and text != _DIALOG_TITLE and page.in_dialog(node):
                    return text
        for _, text, resource_id, _ in page.entries:
            if any(token in resource_id.lower() for token in ("dialog", "error", "reason")):
                if text:
                    return text
        for _, text, _, class_name in page.entries:
            if text and class_name.endswith("TextView") and (
                text.startswith(_DUPLICATE_ERROR) or "logid" in text.lower()
            ):
                return text
        return ""

    @staticmethod
    def _duplicate_tracking(page: _Page, logid: str | None) -> str:
        """Tracking shown by the one exact duplicate body inside the dialog, else ""."""
        if page.dialog_root is None or not logid:
            return ""
        matches = [
            _DUPLICATE_BODY.fullmatch(text)
            for node, text, _, class_name in page.entries
            if text and class_name.endswith("TextView") and page.in_dialog(node)
        ]
        bodies = [match for match in matches if match]
        if len(bodies) != 1 or bodies[0].group(1) != logid:
            return ""
        return bodies[0].group(2)

    @staticmethod
    def _extract_logid(entries: list[Entry]) -> str | None:
        found = {
            match.group(1)
            for _, text, _, _ in entries
            for match in _LOGID.finditer(text)
        }
        return found.pop() if len(found) == 1 else None

    @staticmethod
    def _anchored_records(page: _Page) -> tuple[str, ...] | None:
        anchors = [
            node
            for node, text, _, class_name in page.entries
            if text == _LATEST_ANCHOR and class_name.endswith("TextView")
            and not page.in_dialog(node)
        ]
        if len(anchors) != 1:
            return None
        parent = page.parents.get(anchors[0])
        if parent is None:
            return None
        siblings = [child for child in parent if _visible(child)]
        index = siblings.index(anchors[0])
        following = siblings[index + 1 :]
        if not following or _text(following[0]) != _ALL_RECORDS_LINK:
            return None
        records: list[str] = []
        for row in following[1:]:
            texts = [
                node
                for node in row.iter()
                if node is not row and _visible(node) and _text(node)
            ]
            if (
                _text(row)
                or len(texts) != 1
                or not _is_text_view(texts[0])
                or _looks_like_error(_text(texts[0]))
            ):
                break
            records.append(_text(texts[0]))
        return tuple(records)

    @classmethod
    def _recent_tracking(cls, page: _Page) -> tuple[tuple[str, ...], bool]:
        anchored = cls._anchored_records(page)
        if anchored is not None:
            return anchored, True
        records = []
        for node, text, resource_id, class_name in page.entries:
            if (
                not text
                or class_name.endswith("EditText")
                or not class_name.endswith("TextView")
            ):
                continue
            if any(
                token in text.lower()
                for token in ("error", "失败", _DUPLICATE_ERROR)
            ):
                continue
            normalized_id = resource_id.rsplit("/", 1)[-1]
            is_result_id = normalized_id == "recent_scan_item" or (
                _RECENT_RESOURCE.search(resource_id)
                and re.search(r"(?:item|row|record)", normalized_id, re.IGNORECASE)
            )
            ancestor = page.parents.get(node)
            in_recent_area = False
            while ancestor is not None:
                ancestor_id = ancestor.attrib.get("resource-id", "")
                if _RECENT_RESOURCE.search(ancestor_id):
                    in_recent_area = True
                    break
                ancestor = page.parents.get(ancestor)
            if is_result_id or in_recent_area:
                records.append(text)
        return tuple(records), False

    @staticmethod
    def _classify(
        page: _Page,
        all_text: str,
        labels: dict[str, str],
        inputs: tuple[InputField, ...],
        latest: tuple[str, ...],
        anchored: bool,
        logid: str | None,
        *,
        duplicate: bool,
        has_error: bool,
    ) -> PageState:
        entries = page.entries
        if duplicate and logid:
            return PageState.DUPLICATE_DIALOG
        if page.dialog_root is not None or page.dialog_ambiguous:
            return PageState.UNKNOWN
        ids = " ".join(resource_id.lower() for _, _, resource_id, _ in entries)
        lowered = all_text.lower()
        if (
            "login" in ids
            or "password" in ids
            or any(text in {"登录", "Login", "登录页"} for _, text, _, _ in entries)
        ):
            return PageState.LOGIN
        has_container_menu_title = any(
            text in all_text for text in ("大包管理", "容器管理", "集装箱化", "Container menu")
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
        is_single_sort = any(
            text in _SINGLE_SORT_TITLES for _, text, _, _ in entries
        ) or "single scan" in lowered
        if not is_single_sort:
            return PageState.UNKNOWN
        if len(inputs) != 1 or inputs[0].text:
            return PageState.UNKNOWN
        bag = labels.get("大包号", "").strip()
        sort_code = labels.get("排序码", "").strip()
        if not bag and not sort_code:
            if latest or has_error:
                return PageState.UNKNOWN
            return PageState.SINGLE_EMPTY
        if not sort_code:
            return PageState.UNKNOWN
        if labels.get("扫描状态") in {"扫描失败", "Scan rejected"} and logid and bag:
            return PageState.SCAN_RESULT
        if anchored and latest:
            return PageState.SCAN_RESULT
        if not bag:
            return PageState.UNKNOWN
        success = any(token in all_text for token in ("扫描成功", "Scan successful"))
        if latest and success:
            return PageState.SCAN_RESULT
        return PageState.BAG_READY
