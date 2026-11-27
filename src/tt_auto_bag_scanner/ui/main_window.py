"""Compact, operator-first desktop shell for controlled bag scanning."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tt_auto_bag_scanner.domain.models import DeviceInfo, DeviceMode, ItemStatus, QueueItem

# Site-confirmed report timezone; a saved choice still takes precedence.
DEFAULT_REPORT_TIMEZONE = "America/New_York"
# Carrier/sort pair found in a report that still needs a container number.
PENDING_CARRIER_STATE = "待填大包号"


class PhysicalCheckDialog(QDialog):
    """Require a named operator to attest one parcel/container pairing."""

    def __init__(self, item: QueueItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("实物核验")
        layout = QVBoxLayout(self)
        identifiers = QGridLayout()
        identifiers.addWidget(QLabel("追踪号"), 0, 0)
        identifiers.addWidget(QLabel(item.tracking), 0, 1)
        identifiers.addWidget(QLabel("容器"), 1, 0)
        identifiers.addWidget(QLabel(item.container_no), 1, 1)
        layout.addLayout(identifiers)
        self.operator_edit = QLineEdit()
        self.operator_edit.setPlaceholderText("操作员 ID")
        self.confirm_check = QCheckBox("包裹与容器已核对")
        layout.addWidget(self.operator_edit)
        layout.addWidget(self.confirm_check)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.confirm_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setText("确认")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.confirm_button.setEnabled(False)
        self.operator_edit.textChanged.connect(self._refresh_gate)
        self.confirm_check.toggled.connect(self._refresh_gate)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def operator(self) -> str:
        return self.operator_edit.text().strip()

    def _refresh_gate(self) -> None:
        self.confirm_button.setEnabled(bool(self.operator) and self.confirm_check.isChecked())


class ExcludedRowsDialog(QDialog):
    """Show excluded source rows and offer only validated sort-code choices."""

    def __init__(
        self,
        rows: Iterable[object],
        *,
        selected_overrides: Mapping[int, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("排除项")
        self.choice_combos: dict[int, QComboBox] = {}
        selected = selected_overrides or {}
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["行", "追踪号", "原因", "分拣码"])
        MainWindow._prepare_table(self.table)
        materialized = tuple(rows)
        self.table.setRowCount(len(materialized))
        for row_index, excluded in enumerate(materialized):
            source_row = int(excluded.source_row_number)
            values = (
                str(source_row),
                str(excluded.tracking),
                str(excluded.reason),
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(value))
            choices = tuple(getattr(excluded, "sort_code_choices", ()))
            if choices:
                combo = QComboBox()
                combo.addItem("选择排序码", "")
                for code in choices:
                    combo.addItem(code, code)
                prior = selected.get(source_row)
                if prior:
                    selected_index = combo.findData(prior)
                    if selected_index >= 0:
                        combo.setCurrentIndex(selected_index)
                combo.currentIndexChanged.connect(self._refresh_gate)
                self.choice_combos[source_row] = combo
                self.table.setCellWidget(row_index, 3, combo)
            else:
                self.table.setItem(row_index, 3, QTableWidgetItem("—"))
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.apply_button = QPushButton("应用排序码")
        self.apply_button.setEnabled(False)
        self.close_button = QPushButton("关闭")
        self.apply_button.clicked.connect(self.accept)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.resize(760, 420)
        self._refresh_gate()

    @property
    def selected_sort_overrides(self) -> dict[int, str]:
        return {
            row_number: str(combo.currentData())
            for row_number, combo in self.choice_combos.items()
            if combo.currentData()
        }

    def _refresh_gate(self, *_args: object) -> None:
        self.apply_button.setEnabled(bool(self.selected_sort_overrides))


class ReconciliationDialog(QDialog):
    """Capture an operator decision and its WMS evidence for one unresolved item."""

    def __init__(
        self,
        item: QueueItem,
        attempt: Mapping[str, object],
        *,
        duplicate_recovery: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.duplicate_recovery = duplicate_recovery
        self.setWindowTitle("核对 WMS")
        layout = QVBoxLayout(self)
        info = QGridLayout()
        info.addWidget(QLabel("追踪号"), 0, 0)
        info.addWidget(QLabel(item.tracking), 0, 1)
        info.addWidget(QLabel("容器 / 分拣码"), 1, 0)
        info.addWidget(QLabel(f"{item.container_no} / {item.sort_code}"), 1, 1)
        info.addWidget(QLabel("原 Logid"), 2, 0)
        info.addWidget(QLabel(str(attempt.get("logid") or "—")), 2, 1)
        layout.addLayout(info)

        self.outcome_combo = QComboBox()
        if duplicate_recovery:
            self.outcome_combo.addItem("WMS 已确认运单在当前容器，恢复已完成", "recovered")
        else:
            for status, label in (
                (ItemStatus.SUCCESS, "已确认新扫描"),
                (ItemStatus.ALREADY_IN_BAG, "已确认在当前容器"),
                (ItemStatus.READY, "已确认未提交，可重新核验"),
                (ItemStatus.REJECTED, "已确认拒绝"),
                (ItemStatus.CANCELLED, "已确认作废"),
            ):
                self.outcome_combo.addItem(label, status.value)
        self.operator_edit = QLineEdit()
        self.operator_edit.setPlaceholderText("操作员 ID")
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("处理理由")
        self.evidence_edit = QLineEdit()
        self.evidence_edit.setPlaceholderText("WMS 证据引用 / Logid")
        self.physical_check = QCheckBox("包裹与原目标容器已重新核对")
        if duplicate_recovery:
            self.physical_check.setText("已确认当前 WMS 容器、排序码与追踪号")
        layout.addWidget(self.outcome_combo)
        layout.addWidget(self.operator_edit)
        layout.addWidget(self.reason_edit)
        layout.addWidget(self.evidence_edit)
        layout.addWidget(self.physical_check)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.confirm_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setText("保存核对")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.confirm_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.operator_edit.textChanged.connect(self._refresh_gate)
        self.reason_edit.textChanged.connect(self._refresh_gate)
        self.evidence_edit.textChanged.connect(self._refresh_gate)
        self.outcome_combo.currentIndexChanged.connect(self._refresh_gate)
        self.physical_check.toggled.connect(self._refresh_gate)

    @property
    def decision(self) -> dict[str, object]:
        return {
            "target": self.outcome_combo.currentData(),
            "operator": self.operator_edit.text().strip(),
            "reason": self.reason_edit.text().strip(),
            "evidence_ref": self.evidence_edit.text().strip(),
            "physical_checked": self.physical_check.isChecked(),
        }

    def _refresh_gate(self, *_args: object) -> None:
        decision = self.decision
        needs_physical_check = decision["target"] == ItemStatus.READY.value
        if self.duplicate_recovery:
            needs_physical_check = True
        self.confirm_button.setEnabled(
            bool(decision["operator"])
            and bool(decision["reason"])
            and bool(decision["evidence_ref"])
            and (not needs_physical_check or bool(decision["physical_checked"]))
        )


class PreflightDialog(QDialog):
    """Show the live start gates and require an explicit mode-specific action."""

    def __init__(
        self,
        *,
        mode: DeviceMode,
        device_ready: bool,
        task_ready: bool,
        physical_checks_complete: bool,
        unresolved_count: int,
        real_mode_authorized: bool,
        eligible_count: int,
        verified_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("任务预检")
        layout = QVBoxLayout(self)
        title = QLabel("开始前检查")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"可执行 / 已核验：{eligible_count} / {verified_count}"))
        layout.addWidget(QLabel("模式：模拟" if mode is DeviceMode.SIMULATED else "模式：实机"))
        self.check_rows: dict[str, QLabel] = {}
        checks = (
            ("报表与去重", task_ready),
            ("设备与容器", device_ready),
            ("实物核验", physical_checks_complete),
            ("未解决结果", unresolved_count == 0),
        )
        for name, passed in checks:
            label = QLabel(f"{'PASS' if passed else 'BLOCK'}  {name}")
            label.setObjectName("preflightPass" if passed else "preflightBlock")
            self.check_rows[name] = label
            layout.addWidget(label)
        self.authorization_label = QLabel(
            "PASS  实机授权" if real_mode_authorized else "BLOCK  实机授权"
        )
        self.authorization_label.setObjectName(
            "preflightPass" if real_mode_authorized else "preflightBlock"
        )
        layout.addWidget(self.authorization_label)
        ready = device_ready and task_ready and physical_checks_complete and unresolved_count == 0
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("取消")
        self.start_simulation_button = QPushButton("开始模拟")
        self.start_simulation_button.setObjectName("primaryAction")
        self.start_live_button = QPushButton("开始实扫")
        self.start_live_button.setEnabled(
            ready and mode is DeviceMode.REAL and real_mode_authorized
        )
        self.start_simulation_button.setEnabled(ready and mode is DeviceMode.SIMULATED)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.start_simulation_button)
        actions.addWidget(self.start_live_button)
        layout.addLayout(actions)
        self.cancel_button.clicked.connect(self.reject)
        self.start_simulation_button.clicked.connect(self.accept)
        self.start_live_button.clicked.connect(self.accept)
        self.selected_mode = mode


class MainWindow(QMainWindow):
    """UI emits operator commands; services own all device and database access."""

    open_report_requested = Signal()
    preview_requested = Signal(dict)
    preview_export_requested = Signal(object)
    sort_code_overrides_requested = Signal(object)
    review_task_requested = Signal()
    download_template_requested = Signal()
    excluded_rows_requested = Signal()
    device_refresh_requested = Signal()
    device_connect_requested = Signal(str)
    mode_change_requested = Signal(object)
    real_mode_authorization_requested = Signal(bool)
    physical_check_requested = Signal(str, str)
    cancel_item_requested = Signal(str)
    carrier_save_requested = Signal(dict)
    carrier_delete_requested = Signal(str)
    export_requested = Signal(str)
    diagnostics_requested = Signal()
    container_verify_requested = Signal(str)
    reconciliation_requested = Signal(str)
    import_source_changed = Signal(str, int)
    settings_save_requested = Signal(dict)
    backup_requested = Signal()
    restore_requested = Signal()
    adb_path_browse_requested = Signal()
    history_filter_requested = Signal(dict)
    history_task_selected = Signal(str)
    technical_log_requested = Signal(str)
    start_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()

    page_names = ("扫描工作区", "导入报表", "容器设置", "任务记录", "设置")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("TT Auto Bag Scanner")
        self.resize(1440, 900)
        self.setMinimumSize(1180, 720)
        self._mode = DeviceMode.SIMULATED
        self._real_mode_authorized = False
        self._device_ready = False
        self._task_ready = False
        self._physical_checks_complete = False
        self._unresolved_count = 0
        self._is_running = False
        self._queue_items: list[QueueItem] = []
        # Preview-only rows that still need a container; never queue items.
        self._awaiting_rows: list[object] = []
        self._queue_page_size = 200
        self._workspace_page = 0
        self._preview_page = 0
        self._import_busy = False
        self._excluded_rows: tuple[object, ...] = ()
        self._current_preview: object | None = None
        self._build_ui()
        self._apply_style()
        self._refresh_start_gate()

    def _build_ui(self) -> None:
        root = QWidget(self)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav_layout = QVBoxLayout()
        self.nav_layout.setContentsMargins(16, 18, 12, 12)
        self.nav_layout.setSpacing(4)
        self.nav_buttons: dict[str, QPushButton] = {}
        self.stack = QStackedWidget()
        self._pages: dict[str, QWidget] = {}
        self._build_workspace_page()
        self._build_task_page()
        self._build_device_page()
        self._build_config_page()
        self._build_history_page()
        self._build_diagnostics_page()
        self.threshold_spin.valueChanged.connect(
            lambda value: self.minimum_age_spin.setValue(value)
        )
        self.minimum_age_spin.valueChanged.connect(
            lambda value: self.threshold_spin.setValue(value)
        )
        settings_tabs = QTabWidget()
        settings_tabs.addTab(self._pages.pop("设备"), "PDA 与扫描")
        settings_tabs.addTab(self._pages.pop("诊断"), "诊断")
        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(settings_tabs)
        self._pages["设置"] = settings_page
        brand = QLabel("TT SCANNER")
        brand.setObjectName("wordmark")
        self.nav_warehouse_label = QLabel("仓库未验证")
        self.nav_warehouse_label.setObjectName("eyebrow")
        self.nav_layout.addWidget(brand)
        self.nav_layout.addWidget(self.nav_warehouse_label)
        self.nav_layout.addSpacing(20)
        for index, name in enumerate(self.page_names):
            button = QPushButton(name)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setProperty("page_name", name)
            button.clicked.connect(lambda checked=False, page=name: self.navigate_to(page))
            self.nav_buttons[name] = button
            self.nav_layout.addWidget(button)
            self.stack.addWidget(self._pages[name])
        self.nav_layout.addStretch(1)
        nav_widget = QWidget()
        nav_widget.setLayout(self.nav_layout)
        nav_widget.setFixedWidth(184)
        nav_widget.setObjectName("navigationRail")
        body.addWidget(nav_widget)
        body.addWidget(self.stack, 1)
        root_layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 6, 12, 6)
        bottom.setSpacing(18)
        self.device_status_label = self._status_pill("设备未连接", "muted")
        self.status_mode_label = self._status_pill("模拟 · 不提交 WMS", "safe")
        self.local_status_label = QLabel("本地记录已保存")
        self.help_status_label = QLabel("F1 帮助")
        for widget in (
            self.device_status_label,
            self.status_mode_label,
            self.local_status_label,
            self.help_status_label,
        ):
            bottom.addWidget(widget)
        bottom.addStretch(1)
        self.warehouse_status_label = QLabel("仓库未验证")
        self.container_status_label = QLabel("未开包")
        bottom.addWidget(self.warehouse_status_label)
        bottom.addWidget(self.container_status_label)
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        status_bar.setLayout(bottom)
        root_layout.addWidget(status_bar)

        self.navigate_to("扫描工作区")

    def _build_workspace_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(10)

        heading_row = QHBoxLayout()
        title = QLabel("扫描工作区")
        title.setObjectName("pageTitle")
        self.workspace_task_label = QLabel("未载入任务")
        self.workspace_task_label.setObjectName("subtleValue")
        heading_row.addWidget(title)
        heading_row.addWidget(self.workspace_task_label)
        heading_row.addStretch(1)
        self.start_button = QPushButton("开始模拟")
        self.start_button.setObjectName("primaryAction")
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("停止")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        heading_row.addWidget(self.start_button)
        heading_row.addWidget(self.pause_button)
        heading_row.addWidget(self.stop_button)
        layout.addLayout(heading_row)

        progress_row = QHBoxLayout()
        self.task_progress_label = QLabel("已确认 0 / 0")
        self.task_progress_label.setObjectName("progressValue")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.task_attempted_label = QLabel("已尝试 0 / 0")
        progress_row.addWidget(self.task_progress_label)
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.task_attempted_label)
        layout.addLayout(progress_row)
        self.status_counts_first_label = QLabel("待核验 0 · 待扫描 0 · 提交中 0 · 新扫描 0")
        self.status_counts_second_label = QLabel("已在包中 0 · 待核对 0 · 已拒绝 0 · 已取消 0")
        layout.addWidget(self.status_counts_first_label)
        layout.addWidget(self.status_counts_second_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_table = QTableWidget(0, 5)
        self.workspace_table.setHorizontalHeaderLabels(
            ["追踪号", "承运商", "出库时间", "实物核验", "结果"]
        )
        self._prepare_table(self.workspace_table)
        workspace_queue = QWidget()
        workspace_queue_layout = QVBoxLayout(workspace_queue)
        workspace_queue_layout.setContentsMargins(0, 0, 0, 0)
        workspace_queue_layout.addWidget(self.workspace_table, 1)
        self.workspace_prev_button, self.workspace_next_button, self.workspace_page_label = (
            self._make_queue_pager("workspace")
        )
        workspace_queue_layout.addLayout(
            self._queue_pager_layout(
                self.workspace_prev_button,
                self.workspace_next_button,
                self.workspace_page_label,
            )
        )
        splitter.addWidget(workspace_queue)

        detail_panel = QFrame()
        detail_panel.setObjectName("panel")
        detail_panel.setMinimumWidth(250)
        detail_panel.setMaximumWidth(330)
        detail_layout = QVBoxLayout(detail_panel)
        self.current_bag_title = QLabel("当前容器")
        self.current_bag_title.setObjectName("panelTitle")
        self.bag_session_label = QLabel("WMS 会话未验证")
        self.bag_mapping_label = QLabel("承運商 / 分拣码：—")
        self.bag_container_label = QLabel("容器：—")
        self.bag_verification_label = QLabel("未核验")
        detail_layout.addWidget(self.current_bag_title)
        detail_layout.addWidget(self.bag_session_label)
        detail_layout.addWidget(self.bag_mapping_label)
        detail_layout.addWidget(self.bag_container_label)
        detail_layout.addWidget(self.bag_verification_label)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        detail_layout.addWidget(separator)
        selected_title = QLabel("选中包裹")
        selected_title.setObjectName("panelTitle")
        detail_layout.addWidget(selected_title)
        self.selected_tracking_label = QLabel("未选择")
        self.selected_placement_label = QLabel("实物未核验")
        self.selected_result_label = QLabel("结果：—")
        detail_layout.addWidget(self.selected_tracking_label)
        detail_layout.addWidget(self.selected_placement_label)
        detail_layout.addWidget(self.selected_result_label)
        self.workspace_physical_button = QPushButton("核验实物")
        self.workspace_physical_button.setEnabled(False)
        self.cancel_item_button = QPushButton("取消包裹")
        self.cancel_item_button.setEnabled(False)
        self.review_result_button = QPushButton("核对 WMS 结果")
        self.review_result_button.setEnabled(False)
        detail_layout.addWidget(self.workspace_physical_button)
        detail_layout.addWidget(self.cancel_item_button)
        detail_layout.addWidget(self.review_result_button)
        detail_layout.addStretch(1)
        splitter.addWidget(detail_panel)
        splitter.setSizes([820, 300])
        layout.addWidget(splitter, 1)

        activity_header = QHBoxLayout()
        activity_title = QLabel("活动记录")
        activity_title.setObjectName("panelTitle")
        activity_header.addWidget(activity_title)
        activity_header.addStretch(1)
        self.activity_log_label = QLabel("0 条")
        activity_header.addWidget(self.activity_log_label)
        layout.addLayout(activity_header)
        self.activity_table = QTableWidget(0, 3)
        self.activity_table.setHorizontalHeaderLabels(["时间", "级别", "事件"])
        self._prepare_table(self.activity_table)
        self.activity_table.setMaximumHeight(180)
        layout.addWidget(self.activity_table)

        self.workspace_table.itemSelectionChanged.connect(self._update_workspace_selection)
        self.workspace_physical_button.clicked.connect(
            lambda: self._request_physical_check(self.workspace_table)
        )
        self.cancel_item_button.clicked.connect(self._request_cancel_item)
        self.review_result_button.clicked.connect(self._request_reconciliation)
        self.start_button.clicked.connect(self._open_preflight)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self._pages["扫描工作区"] = page

    def _build_task_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel("导入报表")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        import_panel = QFrame()
        import_panel.setObjectName("panel")
        form = QGridLayout(import_panel)
        form.setContentsMargins(16, 14, 16, 14)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        self.report_path_edit = QLineEdit()
        self.report_path_edit.setPlaceholderText("XLSX 文件")
        self.report_path_edit.setReadOnly(True)
        self.open_report_button = QPushButton("选择文件")
        self.preview_button = QPushButton("生成预览")
        self.download_template_button = QPushButton("下载模板")
        form.addWidget(QLabel("出库报表"), 0, 0)
        form.addWidget(self.report_path_edit, 0, 1, 1, 4)
        form.addWidget(self.download_template_button, 0, 5)
        form.addWidget(self.open_report_button, 0, 6)
        self.sheet_combo = QComboBox()
        self.sheet_combo.addItem("选择工作表", "")
        self.header_row_spin = QDoubleSpinBox()
        self.header_row_spin.setRange(1, 1000000)
        self.header_row_spin.setDecimals(0)
        self.header_row_spin.setValue(1)
        self.header_row_spin.setSuffix(" 行")
        form.addWidget(QLabel("工作表"), 1, 0)
        form.addWidget(self.sheet_combo, 1, 1)
        form.addWidget(QLabel("表头行"), 1, 2)
        form.addWidget(self.header_row_spin, 1, 3)
        self.column_combos: dict[str, QComboBox] = {}
        mapping_fields = (
            ("tracking", "追踪号"),
            ("carrier", "承运商"),
            ("status", "状态"),
            ("outbound_at", "出库时间"),
            ("sort_code", "分拣码（可选）"),
        )
        for col, (key, caption) in enumerate(mapping_fields):
            combo = QComboBox()
            combo.addItem("选择列", "")
            self.column_combos[key] = combo
            row, offset = (2, col) if col < 4 else (3, 0)
            form.addWidget(QLabel(caption), row, offset * 2)
            form.addWidget(combo, row, offset * 2 + 1)
        self.timezone_combo = QComboBox()
        self.timezone_combo.addItem("选择时区", "")
        self.timezone_combo.addItems([])
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0, 1440)
        self.threshold_spin.setDecimals(0)
        self.threshold_spin.setValue(30)
        self.threshold_spin.setSuffix(" 分钟")
        self.confirm_threshold_check = QCheckBox("确认阈值")
        form.addWidget(QLabel("报表时区"), 3, 2)
        form.addWidget(self.timezone_combo, 3, 3)
        form.addWidget(QLabel("筛选阈值"), 3, 4)
        form.addWidget(self.threshold_spin, 3, 5)
        form.addWidget(self.confirm_threshold_check, 3, 6)
        form.addWidget(self.preview_button, 3, 7)
        layout.addWidget(import_panel)

        import_counts = QHBoxLayout()
        self.import_rows_label = QLabel("导入 0 行")
        self.import_eligible_label = QLabel("可执行 0")
        self.import_excluded_label = QLabel("排除 / 重复 0 / 0")
        self.carrier_candidate_counts_label = QLabel("渠道候选：0")
        self.candidate_search_edit = QLineEdit()
        self.candidate_search_edit.setPlaceholderText("追踪号、渠道或容器")
        self.candidate_search_edit.setClearButtonEnabled(True)
        self.preview_export_button = QPushButton("导出预览")
        self.preview_export_button.setEnabled(False)
        for label in (
            self.import_rows_label,
            self.import_eligible_label,
            self.import_excluded_label,
            self.carrier_candidate_counts_label,
        ):
            import_counts.addWidget(label)
        import_counts.addWidget(self.candidate_search_edit)
        import_counts.addWidget(self.preview_export_button)
        import_counts.addStretch(1)
        self.excluded_rows_button = QPushButton("查看排除项")
        import_counts.addWidget(self.excluded_rows_button)
        layout.addLayout(import_counts)

        self.preview_status_label = QLabel("未导入")
        self.preview_status_label.setObjectName("statusLine")
        layout.addWidget(self.preview_status_label)
        self.queue_table = QTableWidget(0, 7)
        self.preview_table = self.queue_table
        self.queue_table.setHorizontalHeaderLabels(
            ["状态", "追踪号", "承运商", "出库时间", "年龄", "容器", "实物核验"]
        )
        self._prepare_table(self.queue_table)
        layout.addWidget(self.queue_table, 1)
        self.preview_prev_button, self.preview_next_button, self.preview_page_label = (
            self._make_queue_pager("preview")
        )
        layout.addLayout(
            self._queue_pager_layout(
                self.preview_prev_button,
                self.preview_next_button,
                self.preview_page_label,
            )
        )
        action_row = QHBoxLayout()
        self.physical_check_button = QPushButton("核验实物")
        self.physical_check_button.setEnabled(False)
        self.item_detail_label = QLabel("未选择包裹")
        self.review_task_button = QPushButton("审核任务")
        self.review_task_button.setEnabled(False)
        action_row.addWidget(self.physical_check_button)
        action_row.addWidget(self.item_detail_label, 1)
        action_row.addWidget(self.review_task_button)
        layout.addLayout(action_row)

        self.open_report_button.clicked.connect(self._choose_report)
        self.preview_button.clicked.connect(self._request_preview)
        self.candidate_search_edit.textChanged.connect(self._filter_preview_candidates)
        self.preview_export_button.clicked.connect(self._emit_preview_export)
        self.sheet_combo.currentIndexChanged.connect(self._import_source_changed)
        self.header_row_spin.valueChanged.connect(self._import_source_changed)
        self.queue_table.itemSelectionChanged.connect(self._update_selected_item)
        self.physical_check_button.clicked.connect(
            lambda: self._request_physical_check(self.queue_table)
        )
        self.download_template_button.clicked.connect(self.download_template_requested.emit)
        self.excluded_rows_button.clicked.connect(self.excluded_rows_requested.emit)
        self.excluded_rows_button.clicked.connect(self._show_excluded_rows)
        self.review_task_button.clicked.connect(self.review_task_requested.emit)
        self._pages["导入报表"] = page

    def _build_device_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("设备与会话")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        panel = QFrame()
        panel.setObjectName("panel")
        form = QGridLayout(panel)
        form.setContentsMargins(18, 18, 18, 18)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        self.device_mode_combo = QComboBox()
        self.device_mode_combo.addItem("模拟", DeviceMode.SIMULATED)
        self.device_mode_combo.addItem("真实设备", DeviceMode.REAL)
        self.device_combo = QComboBox()
        self.device_combo.addItem("未发现设备", "")
        self.device_refresh_button = QPushButton("刷新设备")
        self.device_connect_button = QPushButton("连接")
        self.device_authorize_check = QCheckBox("授权本站实机作业")
        self.device_authorize_check.setChecked(False)
        self.app_version_label = QLabel("WMS 版本未验证")
        self.session_status_label = QLabel("会话未验证")
        self.operator_id_edit = QLineEdit()
        self.operator_id_edit.setPlaceholderText("操作员 ID")
        self.warehouse_id_edit = QLineEdit()
        self.warehouse_id_edit.setPlaceholderText("WMS 仓库标识")
        self.adb_path_edit = QLineEdit()
        self.adb_path_edit.setPlaceholderText("ADB 可执行文件")
        self.adb_path_browse_button = QPushButton("选择")
        self.response_timeout_spin = QDoubleSpinBox()
        self.response_timeout_spin.setRange(1, 120)
        self.response_timeout_spin.setDecimals(0)
        self.response_timeout_spin.setValue(15)
        self.response_timeout_spin.setSuffix(" 秒")
        self.minimum_age_spin = QDoubleSpinBox()
        self.minimum_age_spin.setRange(0, 1440)
        self.minimum_age_spin.setDecimals(0)
        self.minimum_age_spin.setValue(30)
        self.minimum_age_spin.setSuffix(" 分钟")
        self.confirm_minimum_age_check = QCheckBox("确认筛选阈值")
        self.skip_physical_check = QCheckBox("跳过实物核验")
        self.save_settings_button = QPushButton("保存设置")
        self.backup_button = QPushButton("备份本地记录")
        self.restore_button = QPushButton("恢复记录")
        form.addWidget(QLabel("运行模式"), 0, 0)
        form.addWidget(self.device_mode_combo, 0, 1)
        form.addWidget(QLabel("设备"), 1, 0)
        form.addWidget(self.device_combo, 1, 1, 1, 2)
        form.addWidget(self.device_refresh_button, 1, 3)
        form.addWidget(self.device_connect_button, 1, 4)
        form.addWidget(self.device_authorize_check, 2, 1, 1, 3)
        form.addWidget(self.app_version_label, 3, 1, 1, 3)
        form.addWidget(self.session_status_label, 4, 1, 1, 3)
        form.addWidget(QLabel("操作员"), 5, 0)
        form.addWidget(self.operator_id_edit, 5, 1, 1, 2)
        form.addWidget(QLabel("仓库"), 5, 3)
        form.addWidget(self.warehouse_id_edit, 5, 4)
        form.addWidget(QLabel("响应超时"), 6, 0)
        form.addWidget(self.response_timeout_spin, 6, 1)
        form.addWidget(QLabel("最小出库时间"), 6, 2)
        form.addWidget(self.minimum_age_spin, 6, 3)
        form.addWidget(self.confirm_minimum_age_check, 6, 4)
        form.addWidget(self.skip_physical_check, 7, 1, 1, 2)
        form.addWidget(self.save_settings_button, 7, 4)
        form.addWidget(self.backup_button, 8, 3)
        form.addWidget(self.restore_button, 8, 4)
        form.addWidget(QLabel("ADB"), 9, 0)
        form.addWidget(self.adb_path_edit, 9, 1, 1, 3)
        form.addWidget(self.adb_path_browse_button, 9, 4)
        layout.addWidget(panel)
        layout.addStretch(1)
        self.device_refresh_button.clicked.connect(self.device_refresh_requested.emit)
        self.device_connect_button.clicked.connect(self._request_connection)
        self.device_mode_combo.currentIndexChanged.connect(self._mode_selected)
        self.device_authorize_check.toggled.connect(self.real_mode_authorization_requested.emit)
        self.save_settings_button.clicked.connect(self._save_settings)
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.restore_button.clicked.connect(self.restore_requested.emit)
        self.adb_path_browse_button.clicked.connect(self.adb_path_browse_requested.emit)
        self._pages["设备"] = page

    def _build_config_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("承运商配置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        panel = QFrame()
        panel.setObjectName("panel")
        form = QGridLayout(panel)
        form.setContentsMargins(16, 14, 16, 14)
        self.carrier_key_edit = QLineEdit()
        self.carrier_key_edit.setPlaceholderText("配置键")
        self.carrier_name_edit = QLineEdit()
        self.carrier_name_edit.setPlaceholderText("显示名称")
        self.carrier_aliases_edit = QLineEdit()
        self.carrier_aliases_edit.setPlaceholderText("别名，以逗号分隔")
        self.sort_code_edit = QLineEdit()
        self.sort_code_edit.setPlaceholderText("分拣码")
        self.container_edit = QLineEdit()
        self.container_edit.setPlaceholderText("容器号")
        self.carrier_enabled_check = QCheckBox("启用")
        self.carrier_enabled_check.setChecked(True)
        self.carrier_save_button = QPushButton("保存")
        self.container_verify_button = QPushButton("PDA 验证")
        form.addWidget(self.carrier_key_edit, 0, 0)
        form.addWidget(self.carrier_name_edit, 0, 1)
        form.addWidget(self.carrier_aliases_edit, 1, 0, 1, 2)
        form.addWidget(self.sort_code_edit, 2, 0)
        form.addWidget(self.container_edit, 2, 1)
        form.addWidget(self.carrier_enabled_check, 3, 0)
        form.addWidget(self.container_verify_button, 3, 1)
        form.addWidget(self.carrier_save_button, 3, 2)
        layout.addWidget(panel)
        self.carrier_table = QTableWidget(0, 6)
        self.carrier_table.setHorizontalHeaderLabels(
            ["键", "承运商", "别名", "分拣码", "容器", "状态"]
        )
        self._prepare_table(self.carrier_table)
        layout.addWidget(self.carrier_table, 1)
        self.carrier_save_button.clicked.connect(self._save_carrier)
        self.container_verify_button.clicked.connect(self._verify_container_mapping)
        self.carrier_table.itemSelectionChanged.connect(self._populate_carrier_editor)
        self._pages["容器设置"] = page

    def _build_history_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        heading_row = QHBoxLayout()
        title = QLabel("任务记录")
        title.setObjectName("pageTitle")
        heading_row.addWidget(title)
        heading_row.addStretch(1)
        self.export_csv_button = QPushButton("导出 CSV")
        self.export_xlsx_button = QPushButton("导出 XLSX")
        heading_row.addWidget(self.export_csv_button)
        heading_row.addWidget(self.export_xlsx_button)
        layout.addLayout(heading_row)
        filter_row = QHBoxLayout()
        self.history_search_edit = QLineEdit()
        self.history_search_edit.setPlaceholderText("批次或追踪号")
        self.history_status_combo = QComboBox()
        for value, label in (
            ("", "全部状态"),
            ("ready", "待开始"),
            ("running", "运行中"),
            ("paused", "已暂停"),
            ("blocked", "已阻塞"),
            ("completed", "已完成"),
            ("stopped", "已停止"),
        ):
            self.history_status_combo.addItem(label, value)
        self.history_date_from = QDateEdit(QDate.currentDate())
        self.history_date_from.setCalendarPopup(True)
        self.history_date_to = QDateEdit(QDate.currentDate())
        self.history_date_to.setCalendarPopup(True)
        self.history_filter_button = QPushButton("筛选")
        for label, widget in (
            ("查找", self.history_search_edit),
            ("状态", self.history_status_combo),
            ("从", self.history_date_from),
            ("到", self.history_date_to),
        ):
            filter_row.addWidget(QLabel(label))
            filter_row.addWidget(widget)
        filter_row.addWidget(self.history_filter_button)
        layout.addLayout(filter_row)
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ["批次 ID", "创建时间", "处理数", "新扫描", "重复", "待核对", "状态"]
        )
        self._prepare_table(self.history_table)
        layout.addWidget(self.history_table, 1)
        detail_title = QLabel("待核对结果")
        detail_title.setObjectName("panelTitle")
        layout.addWidget(detail_title)
        self.unresolved_table = QTableWidget(0, 5)
        self.unresolved_table.setHorizontalHeaderLabels(
            ["追踪号", "承运商", "异常", "Log ID", "下一步"]
        )
        self._prepare_table(self.unresolved_table)
        self.unresolved_table.setMaximumHeight(180)
        layout.addWidget(self.unresolved_table)
        detail_actions = QHBoxLayout()
        self.open_reconciliation_button = QPushButton("打开核对")
        self.open_reconciliation_button.setEnabled(False)
        self.export_batch_button = QPushButton("导出批次")
        self.technical_log_button = QPushButton("技术日志")
        detail_actions.addWidget(self.open_reconciliation_button)
        detail_actions.addWidget(self.export_batch_button)
        detail_actions.addWidget(self.technical_log_button)
        detail_actions.addStretch(1)
        layout.addLayout(detail_actions)
        self.export_csv_button.clicked.connect(lambda: self.export_requested.emit("csv"))
        self.export_xlsx_button.clicked.connect(lambda: self.export_requested.emit("xlsx"))
        self.history_filter_button.clicked.connect(self._request_history_filter)
        self.history_table.itemSelectionChanged.connect(self._selected_history_task)
        self.unresolved_table.itemSelectionChanged.connect(self._update_reconciliation_button)
        self.open_reconciliation_button.clicked.connect(self._open_selected_reconciliation)
        self.export_batch_button.clicked.connect(lambda: self.export_requested.emit("batch"))
        self.technical_log_button.clicked.connect(self._open_technical_log)
        self._pages["任务记录"] = page

    def _build_diagnostics_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("诊断")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(18, 18, 18, 18)
        self.diagnostic_labels: dict[str, QLabel] = {}
        checks = (
            ("adb", "ADB"),
            ("driver", "LibUSB"),
            ("database", "本地数据库"),
            ("device", "设备授权"),
            ("wms", "WMS 会话"),
            ("wms_version", "WMS 版本"),
        )
        for row, (key, caption) in enumerate(checks):
            grid.addWidget(QLabel(caption), row, 0)
            value = QLabel("未检查")
            value.setObjectName("diagnosticValue")
            self.diagnostic_labels[key] = value
            grid.addWidget(value, row, 1)
        self.run_diagnostics_button = QPushButton("运行检查")
        grid.addWidget(self.run_diagnostics_button, len(checks), 1)
        layout.addWidget(panel)
        layout.addStretch(1)
        self.run_diagnostics_button.clicked.connect(self.diagnostics_requested.emit)
        self._pages["诊断"] = page

    @staticmethod
    def _prepare_table(table: QTableWidget) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setMinimumSectionSize(72)

    def _make_queue_pager(self, table_key: str) -> tuple[QPushButton, QPushButton, QLabel]:
        previous = QPushButton("上一页")
        following = QPushButton("下一页")
        label = QLabel("第 0 / 0 页 · 0 项")
        previous.clicked.connect(lambda: self._change_queue_page(table_key, -1))
        following.clicked.connect(lambda: self._change_queue_page(table_key, 1))
        previous.setEnabled(False)
        following.setEnabled(False)
        return previous, following, label

    @staticmethod
    def _queue_pager_layout(
        previous: QPushButton,
        following: QPushButton,
        label: QLabel,
    ) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.addStretch(1)
        layout.addWidget(previous)
        layout.addWidget(label)
        layout.addWidget(following)
        return layout

    def _change_queue_page(self, table_key: str, delta: int) -> None:
        if table_key == "workspace":
            self._workspace_page = max(0, self._workspace_page + delta)
        else:
            self._preview_page = max(0, self._preview_page + delta)
        self._render_queue_tables()

    @staticmethod
    def _status_pill(text: str, kind: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("statusPill")
        label.setProperty("kind", kind)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    @staticmethod
    def _status_text(status: str) -> str:
        labels = {
            "wait_physical": "待核验",
            "ready": "待扫描",
            "in_flight": "提交中",
            "success": "已扫描",
            "already_in_bag": "已在包中",
            "unknown": "待核对",
            "rejected": "已拒绝",
            "cancelled": "已取消",
            "awaiting_container": PENDING_CARRIER_STATE,
        }
        return labels.get(status, status)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #ffffff;
                color: #19232c;
            }
            QWidget {
                font-family: 'Segoe UI';
                font-size: 13px;
                color: #19232c;
            }
            QDialog, QMessageBox {
                background: #ffffff;
                color: #19232c;
            }
            QMessageBox QLabel { background: transparent; color: #19232c; }
            QWidget#navigationRail {
                background: #f6f7f8;
                border-right: 1px solid #dce2e5;
            }
            QWidget#statusBar {
                background: #f2f4f5;
                border-top: 1px solid #dfe4e6;
            }
            QLabel#wordmark {
                font-size: 19px;
                font-weight: 800;
                letter-spacing: 1px;
                color: #0a3c45;
            }
            QLabel#eyebrow { color: #657780; font-size: 11px; }
            QLabel#pageTitle {
                font-size: 21px;
                font-weight: 700;
                color: #152a31;
                padding: 2px 0 4px;
            }
            QLabel#statusPill {
                background: #dce4e8;
                color: #38515b;
                border-radius: 11px;
                padding: 7px 12px;
                font-size: 12px;
                font-weight: 650;
            }
            QLabel#statusPill[kind="safe"] { background: #d8eee5; color: #155e48; }
            QLabel#statusPill[kind="warning"] { background: #f7e8c7; color: #78520c; }
            QLabel#statusPill[kind="danger"] { background: #f5dddd; color: #873b3b; }
            QLabel#statusPill[kind="muted"] { background: #e1e7ea; color: #667981; }
            QFrame#separator { color: #d7dfe2; }
            QFrame#panel {
                background: #ffffff;
                border: 1px solid #dce4e7;
                border-radius: 8px;
            }
            QPushButton {
                background: #e3e9eb;
                border: 0;
                border-radius: 6px;
                padding: 9px 14px;
                font-weight: 600;
            }
            QPushButton:hover { background: #d4dfe2; }
            QPushButton:disabled { background: #e9edef; color: #98a5aa; }
            QPushButton#primaryAction {
                background: #116b61;
                color: #ffffff;
                padding: 10px 20px;
            }
            QPushButton#primaryAction:hover { background: #0d5a52; }
            QPushButton#primaryAction:disabled { background: #cdd9d8; color: #f3f6f5; }
            QPushButton#navButton {
                text-align: left;
                background: transparent;
                padding: 11px 12px;
                color: #546870;
            }
            QPushButton#navButton:checked {
                background: #d6e6e7;
                color: #0b5155;
                font-weight: 700;
            }
            QLineEdit, QComboBox, QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #cdd9dd;
                border-radius: 5px;
                padding: 7px 9px;
                min-height: 20px;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #31877c;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f6f8f8;
                border: 1px solid #dce4e7;
                border-radius: 6px;
                gridline-color: #edf1f2;
                selection-background-color: #d4e7e7;
                selection-color: #173c3d;
            }
            QHeaderView::section {
                background: #f1f4f5;
                color: #566a71;
                border: 0;
                padding: 8px;
                font-weight: 700;
            }
            QLabel#progressValue {
                font-size: 16px;
                font-weight: 750;
                color: #123f45;
            }
            QLabel#statusLine { color: #547079; font-weight: 600; }
            QLabel#diagnosticValue { color: #356d5b; font-weight: 650; }
            QLabel#preflightPass { color: #1f765a; font-weight: 650; }
            QLabel#preflightBlock { color: #a35d20; font-weight: 650; }
            QLabel#panelTitle { color: #263940; font-size: 14px; font-weight: 700; }
            QLabel#subtleValue { color: #697c83; font-size: 12px; }
            QProgressBar {
                background: #e8edef;
                border: 0;
                border-radius: 3px;
                min-height: 6px;
                max-height: 6px;
            }
            QProgressBar::chunk { background: #23738b; border-radius: 3px; }
            QTabWidget::pane { border: 0; }
            QTabBar::tab {
                background: #f4f6f7;
                color: #566970;
                padding: 8px 16px;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #e1ebf8; color: #194a78; font-weight: 700; }
            """
        )

    def _choose_report(self) -> None:
        self.open_report_requested.emit()

    def _request_preview(self) -> None:
        mapping: dict[str, object] = {
            key: combo.currentData() for key, combo in self.column_combos.items()
        }
        mapping["report_timezone"] = self.timezone_combo.currentData()
        mapping["threshold_minutes"] = int(self.threshold_spin.value())
        mapping["threshold_confirmed"] = self.confirm_threshold_check.isChecked()
        mapping["path"] = self.report_path_edit.text()
        mapping["sheet_name"] = self.sheet_combo.currentData()
        mapping["header_row"] = int(self.header_row_spin.value())
        required = ("tracking", "carrier", "status", "outbound_at")
        if (
            not mapping["path"]
            or not mapping["sheet_name"]
            or not mapping["report_timezone"]
            or not mapping["threshold_confirmed"]
            or any(not mapping[key] for key in required)
        ):
            self.preview_status_label.setText("确认文件、字段、时区与阈值")
            return
        self.preview_requested.emit(mapping)

    def _request_connection(self) -> None:
        self.device_connect_requested.emit(str(self.device_combo.currentData() or ""))

    def _mode_selected(self, index: int) -> None:
        mode = self.device_mode_combo.itemData(index)
        if mode is not None:
            self.mode_change_requested.emit(mode)

    def _request_physical_check(self, table: QTableWidget) -> None:
        row = table.currentRow()
        index = self._table_global_index(table, row)
        if index is None:
            return
        item = self._items_for_table(table)[index]
        dialog = PhysicalCheckDialog(item, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.physical_check_requested.emit(item.id, dialog.operator)

    def _update_selected_item(self) -> None:
        row = self.queue_table.currentRow()
        index = self._table_global_index(self.queue_table, row)
        if index is not None:
            item = self._items_for_table(self.queue_table)[index]
            self.item_detail_label.setText(item.tracking)
            self.physical_check_button.setEnabled(
                not self._is_running and item.status.value in {"wait_physical", "ready"}
            )
        else:
            self.item_detail_label.setText("未选择包裹")
            self.physical_check_button.setEnabled(False)

    def _update_workspace_selection(self) -> None:
        row = self.workspace_table.currentRow()
        index = self._table_global_index(self.workspace_table, row)
        if index is None:
            self.selected_tracking_label.setText("未选择")
            self.selected_placement_label.setText("实物未核验")
            self.selected_result_label.setText("结果：—")
            self.workspace_physical_button.setEnabled(False)
            self.cancel_item_button.setEnabled(False)
            self.review_result_button.setEnabled(False)
            return
        item = self._queue_items[index]
        self.selected_tracking_label.setText(item.tracking)
        checked = item.physical_check_id is not None or item.status.value not in {
            "wait_physical",
            "ready",
        }
        self.selected_placement_label.setText("实物已核验" if checked else "实物未核验")
        self.selected_result_label.setText(f"结果：{self._status_text(item.status.value)}")
        self.workspace_physical_button.setEnabled(
            not self._is_running and item.status.value in {"wait_physical", "ready"}
        )
        self.cancel_item_button.setEnabled(
            not self._is_running and item.status in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}
        )
        self.review_result_button.setEnabled(
            not self._is_running and item.status.value == "unknown"
        )

    def _update_reconciliation_button(self, *_args: object) -> None:
        self.open_reconciliation_button.setEnabled(
            not self._is_running and self.unresolved_table.currentRow() >= 0
        )

    def _request_reconciliation(self) -> None:
        row = self.workspace_table.currentRow()
        index = self._table_global_index(self.workspace_table, row)
        if index is not None:
            self.reconciliation_requested.emit(self._queue_items[index].id)

    def _request_cancel_item(self) -> None:
        row = self.workspace_table.currentRow()
        index = self._table_global_index(self.workspace_table, row)
        if index is None or self._is_running:
            return
        item = self._queue_items[index]
        if item.status not in {ItemStatus.WAIT_PHYSICAL, ItemStatus.READY}:
            return
        answer = QMessageBox.question(
            self,
            "取消包裹",
            f"取消 {item.tracking}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.cancel_item_requested.emit(item.id)

    def _table_global_index(self, table: QTableWidget, row: int) -> int | None:
        if row < 0:
            return None
        page = self._workspace_page if table is self.workspace_table else self._preview_page
        index = page * self._queue_page_size + row
        return index if index < len(self._items_for_table(table)) else None

    def _items_for_table(self, table: QTableWidget) -> list[QueueItem]:
        return self._queue_items if table is self.workspace_table else self._preview_visible_items()

    def _preview_visible_items(self) -> list[QueueItem]:
        query = self.candidate_search_edit.text().strip().casefold()
        rows = [*self._queue_items, *self._awaiting_rows]
        if not query:
            return rows
        return [
            item
            for item in rows
            if any(
                query in value.casefold()
                for value in (
                    item.tracking,
                    item.carrier_key,
                    item.sort_code,
                    item.container_no,
                    *(str(row_number) for row_number in item.source_row_numbers),
                )
            )
        ]

    def _open_preflight(self) -> None:
        verified_count = sum(
            item.status.value == "ready" or item.physical_check_id is not None
            for item in self._queue_items
        )
        dialog = PreflightDialog(
            mode=self._mode,
            device_ready=self._device_ready,
            task_ready=self._task_ready,
            physical_checks_complete=self._physical_checks_complete,
            unresolved_count=self._unresolved_count,
            real_mode_authorized=self._real_mode_authorized,
            eligible_count=len(self._queue_items),
            verified_count=verified_count,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.start_requested.emit()

    def _show_excluded_rows(self) -> None:
        mapping = getattr(getattr(self._current_preview, "summary", None), "mapping", None)
        selected = dict(getattr(mapping, "sort_code_overrides", ()))
        dialog = ExcludedRowsDialog(
            self._excluded_rows,
            selected_overrides=selected,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_sort_overrides:
            return
        selected.update(dialog.selected_sort_overrides)
        self.sort_code_overrides_requested.emit(selected)

    def _filter_preview_candidates(self, *_args: object) -> None:
        self._preview_page = 0
        self._render_queue_tables()
        self._update_selected_item()
        self._refresh_carrier_candidate_counts()

    def _emit_preview_export(self) -> None:
        if self._current_preview is not None:
            self.preview_export_requested.emit(self._current_preview)

    def _refresh_carrier_candidate_counts(self) -> None:
        counts = Counter(item.carrier_key for item in self._preview_visible_items())
        rendered = " · ".join(
            f"{carrier} {count}"
            for carrier, count in sorted(counts.items(), key=lambda pair: pair[0].casefold())
        )
        self.carrier_candidate_counts_label.setText(f"渠道候选：{rendered or '0'}")

    def _populate_carrier_editor(self) -> None:
        row = self.carrier_table.currentRow()
        if row < 0:
            return
        values = [self.carrier_table.item(row, column) for column in range(6)]
        if any(value is None for value in values):
            return
        self.carrier_key_edit.setText(values[0].text())
        self.carrier_name_edit.setText(values[1].text())
        self.carrier_aliases_edit.setText(values[2].text())
        self.sort_code_edit.setText(values[3].text())
        self.container_edit.setText(values[4].text())
        pending = values[5].text() == PENDING_CARRIER_STATE
        self.carrier_enabled_check.setChecked(pending or values[5].text() == "启用")
        if pending:
            self.container_edit.setFocus()

    def _verify_container_mapping(self) -> None:
        self.container_verify_requested.emit(self.carrier_key_edit.text().strip())

    def _save_settings(self) -> None:
        self.settings_save_requested.emit(
            {
                "operator_id": self.operator_id_edit.text().strip(),
                "warehouse": self.warehouse_id_edit.text().strip(),
                "adb_path": self.adb_path_edit.text().strip(),
                "response_timeout_seconds": int(self.response_timeout_spin.value()),
                "minimum_age_minutes": int(self.minimum_age_spin.value()),
                "minimum_age_confirmed": self.confirm_minimum_age_check.isChecked(),
                "skip_physical_check": self.skip_physical_check.isChecked(),
                "real_mode_authorized": self.device_authorize_check.isChecked(),
                "mode": self._mode.value,
            }
        )

    def confirm_physical_skip(self, count: int, operator: str) -> str | None:
        """Ask the operator to authorize skipping; returns their name or None."""
        name, accepted = QInputDialog.getText(
            self,
            "跳过实物核验",
            f"{count} 项未核验，将以“授权跳过”记录并允许提交 WMS。操作员：",
            QLineEdit.EchoMode.Normal,
            operator,
        )
        name = name.strip()
        return name if accepted and name else None

    def set_settings(self, settings: Mapping[str, object]) -> None:
        self.operator_id_edit.setText(str(settings.get("operator_id", "")))
        self.warehouse_id_edit.setText(str(settings.get("warehouse", "")))
        self.adb_path_edit.setText(str(settings.get("adb_path", "")))
        self.response_timeout_spin.setValue(float(settings.get("response_timeout_seconds", 15)))
        self.minimum_age_spin.setValue(float(settings.get("minimum_age_minutes", 30)))
        self.confirm_minimum_age_check.setChecked(
            bool(settings.get("minimum_age_confirmed", False))
        )
        self.skip_physical_check.setChecked(settings.get("skip_physical_check") is True)
        authorized = bool(settings.get("real_mode_authorized", False))
        self.device_authorize_check.blockSignals(True)
        self.device_authorize_check.setChecked(authorized)
        self.device_authorize_check.blockSignals(False)

    def _request_history_filter(self) -> None:
        self.history_filter_requested.emit(
            {
                "query": self.history_search_edit.text().strip(),
                "state": self.history_status_combo.currentData(),
                "date_from": self.history_date_from.date().toString("yyyy-MM-dd"),
                "date_to": self.history_date_to.date().toString("yyyy-MM-dd"),
            }
        )

    def _selected_history_task(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            return
        cell = self.history_table.item(row, 0)
        if cell is not None:
            task_id = cell.data(Qt.ItemDataRole.UserRole) or cell.text()
            self.history_task_selected.emit(str(task_id))

    def _open_selected_reconciliation(self) -> None:
        row = self.unresolved_table.currentRow()
        if row < 0:
            return
        cell = self.unresolved_table.item(row, 0)
        if cell is not None:
            self.reconciliation_requested.emit(str(cell.data(Qt.ItemDataRole.UserRole)))

    def open_reconciliation_dialog(
        self,
        item: QueueItem,
        attempt: Mapping[str, object],
        *,
        duplicate_recovery: bool = False,
    ) -> dict[str, object] | None:
        dialog = ReconciliationDialog(
            item,
            attempt,
            duplicate_recovery=duplicate_recovery,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.decision

    def _open_technical_log(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            return
        cell = self.history_table.item(row, 0)
        if cell is not None:
            self.technical_log_requested.emit(
                str(cell.data(Qt.ItemDataRole.UserRole) or cell.text())
            )

    def _save_carrier(self) -> None:
        self.carrier_save_requested.emit(
            {
                "carrier_key": self.carrier_key_edit.text().strip(),
                "display_name": self.carrier_name_edit.text().strip(),
                "aliases": tuple(
                    alias.strip()
                    for alias in self.carrier_aliases_edit.text().split(",")
                    if alias.strip()
                ),
                "sort_code": self.sort_code_edit.text().strip(),
                "container_no": self.container_edit.text().strip(),
                "enabled": self.carrier_enabled_check.isChecked(),
            }
        )

    def navigate_to(self, page_name: str) -> None:
        if page_name not in self.page_names:
            raise ValueError(f"unknown page: {page_name}")
        index = self.page_names.index(page_name)
        self.stack.setCurrentIndex(index)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == page_name)

    @property
    def current_page_name(self) -> str:
        return self.page_names[self.stack.currentIndex()]

    def set_start_conditions(
        self,
        *,
        device_ready: bool,
        task_ready: bool,
        physical_checks_complete: bool,
        unresolved_count: int,
        mode: DeviceMode,
        real_mode_authorized: bool = False,
    ) -> None:
        self._device_ready = device_ready
        self._task_ready = task_ready
        self._physical_checks_complete = physical_checks_complete
        self._unresolved_count = unresolved_count
        self._mode = mode
        self._real_mode_authorized = real_mode_authorized
        self.set_mode(mode)
        self._refresh_start_gate()

    def set_running(self, running: bool) -> None:
        self._is_running = running
        self.pause_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        for widget in (
            self.device_mode_combo,
            self.device_combo,
            self.device_refresh_button,
            self.device_connect_button,
            self.device_authorize_check,
            self.save_settings_button,
            self.restore_button,
            self.open_report_button,
            self.preview_button,
            self.review_task_button,
            self.carrier_save_button,
            self.container_verify_button,
            self.physical_check_button,
            self.workspace_physical_button,
            self.cancel_item_button,
            self.review_result_button,
            self.open_reconciliation_button,
        ):
            widget.setEnabled(not running)
        self.set_import_busy(self._import_busy)
        self._update_selected_item()
        self._update_workspace_selection()
        self._update_reconciliation_button()
        self._refresh_start_gate()

    def _refresh_start_gate(self) -> None:
        self.start_button.setEnabled(
            self._device_ready
            and self._task_ready
            and self._physical_checks_complete
            and self._unresolved_count == 0
            and not self._is_running
            and (self._mode is DeviceMode.SIMULATED or self._real_mode_authorized)
        )

    def set_mode(self, mode: DeviceMode) -> None:
        self._mode = mode
        self.status_mode_label.setText(
            "模拟 · 不提交 WMS" if mode is DeviceMode.SIMULATED else "实机"
        )
        self.status_mode_label.setProperty(
            "kind", "safe" if mode is DeviceMode.SIMULATED else "warning"
        )
        self.start_button.setText("开始模拟" if mode is DeviceMode.SIMULATED else "开始实扫")
        self.status_mode_label.style().unpolish(self.status_mode_label)
        self.status_mode_label.style().polish(self.status_mode_label)
        index = self.device_mode_combo.findData(mode)
        if index >= 0 and index != self.device_mode_combo.currentIndex():
            self.device_mode_combo.blockSignals(True)
            self.device_mode_combo.setCurrentIndex(index)
            self.device_mode_combo.blockSignals(False)
        self._refresh_start_gate()

    def set_report_path(self, path: str) -> None:
        self.report_path_edit.setText(path)

    def set_import_headers(
        self,
        headers: Iterable[str],
        *,
        selected_columns: Mapping[str, object] | None = None,
    ) -> None:
        selected = selected_columns or {}
        for key, combo in self.column_combos.items():
            combo.clear()
            combo.addItem("选择列", "")
            for header in headers:
                combo.addItem(header, header)
            selected_header = selected.get(key)
            if selected_header:
                index = combo.findData(str(selected_header))
                if index >= 0:
                    combo.setCurrentIndex(index)

    def apply_import_defaults(self, defaults: Mapping[str, object]) -> None:
        timezone = str(defaults.get("report_timezone") or DEFAULT_REPORT_TIMEZONE)
        timezone_index = self.timezone_combo.findData(timezone)
        if timezone_index >= 0:
            self.timezone_combo.setCurrentIndex(timezone_index)

        try:
            threshold_minutes = int(defaults.get("threshold_minutes", 30))
        except (TypeError, ValueError):
            return
        if 0 <= threshold_minutes <= 1440:
            self.threshold_spin.setValue(threshold_minutes)

    def set_import_busy(self, busy: bool) -> None:
        self._import_busy = busy
        enabled = not busy and not self._is_running
        for widget in (
            self.open_report_button,
            self.preview_button,
            self.download_template_button,
            self.restore_button,
            self.sheet_combo,
            self.header_row_spin,
            self.timezone_combo,
            self.threshold_spin,
            self.confirm_threshold_check,
            self.candidate_search_edit,
            *self.column_combos.values(),
        ):
            widget.setEnabled(enabled)
        self.preview_export_button.setEnabled(
            enabled and self._current_preview is not None
        )

    def set_import_sheets(self, sheets: Iterable[str], selected: str | None = None) -> None:
        names = tuple(sheets)
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        self.sheet_combo.addItem("选择工作表", "")
        for name in names:
            self.sheet_combo.addItem(name, name)
        index = self.sheet_combo.findData(selected or (names[0] if names else ""))
        if index >= 0:
            self.sheet_combo.setCurrentIndex(index)
        self.sheet_combo.blockSignals(False)

    def _import_source_changed(self, *_args: object) -> None:
        sheet_name = str(self.sheet_combo.currentData() or "")
        if self.report_path_edit.text() and sheet_name:
            self.import_source_changed.emit(sheet_name, int(self.header_row_spin.value()))

    def set_timezones(self, timezones: Iterable[str]) -> None:
        self.timezone_combo.clear()
        self.timezone_combo.addItem("选择时区", "")
        for timezone in timezones:
            self.timezone_combo.addItem(timezone, timezone)

    def set_devices(self, devices: Iterable[DeviceInfo]) -> None:
        self.device_combo.clear()
        found = tuple(devices)
        if not found:
            self.device_combo.addItem("未发现设备", "")
            return
        for device in found:
            self.device_combo.addItem(
                f"{device.model or 'Android'} · {device.serial}", device.serial
            )

    def set_device_status(
        self,
        *,
        connected: bool,
        warehouse: str = "",
        container_no: str = "",
        app_version: str = "",
        session_verified: bool = False,
    ) -> None:
        self.device_status_label.setText("设备已连接" if connected else "设备未连接")
        self.device_status_label.setProperty("kind", "safe" if connected else "muted")
        self.warehouse_status_label.setText(warehouse or "仓库未验证")
        self.nav_warehouse_label.setText(warehouse or "仓库未验证")
        self.warehouse_status_label.setProperty("kind", "safe" if warehouse else "muted")
        self.container_status_label.setText(container_no or "未开包")
        self.container_status_label.setProperty("kind", "safe" if container_no else "muted")
        self.app_version_label.setText(app_version or "WMS 版本未验证")
        self.session_status_label.setText("会话有效" if session_verified else "会话未验证")
        self.bag_session_label.setText("WMS 会话有效" if session_verified else "WMS 会话未验证")
        self.bag_container_label.setText(f"容器：{container_no or '—'}")
        self.bag_verification_label.setText("容器与分拣码已验证" if session_verified else "未核验")
        for label in (
            self.device_status_label,
            self.warehouse_status_label,
            self.container_status_label,
        ):
            label.style().unpolish(label)
            label.style().polish(label)

    def set_queue_items(self, items: Iterable[QueueItem]) -> None:
        self._queue_items = list(items)
        self._workspace_page = min(
            self._workspace_page,
            max(0, (len(self._queue_items) - 1) // self._queue_page_size),
        )
        visible_preview_items = self._preview_visible_items()
        self._preview_page = min(
            self._preview_page,
            max(0, (len(visible_preview_items) - 1) // self._queue_page_size),
        )
        self._render_queue_tables()
        self._refresh_carrier_candidate_counts()
        self.review_task_button.setEnabled(bool(self._queue_items) and not self._is_running)
        self._update_selected_item()
        self._update_workspace_selection()

    def _render_queue_tables(self) -> None:
        now = datetime.now(UTC)
        specs = (
            (
                self.queue_table,
                self._preview_visible_items(),
                self._preview_page,
                self.preview_page_label,
                self.preview_prev_button,
                self.preview_next_button,
                True,
            ),
            (
                self.workspace_table,
                self._queue_items,
                self._workspace_page,
                self.workspace_page_label,
                self.workspace_prev_button,
                self.workspace_next_button,
                False,
            ),
        )
        for table, items, page, label, previous, following, preview in specs:
            page_count = (len(items) + self._queue_page_size - 1) // self._queue_page_size
            selected_item_id = ""
            selected_row = table.currentRow()
            if selected_row >= 0:
                selected_cell = table.item(selected_row, 0)
                if selected_cell is not None:
                    selected_item_id = str(selected_cell.data(Qt.ItemDataRole.UserRole) or "")
            start = page * self._queue_page_size
            page_items = items[start : start + self._queue_page_size]
            table.setRowCount(len(page_items))
            for row, item in enumerate(page_items):
                age_minutes = max(
                    0,
                    int((now - item.outbound_at_utc.astimezone(UTC)).total_seconds() // 60),
                )
                checked = item.physical_check_id is not None or item.status.value not in {
                    "wait_physical",
                    "ready",
                }
                if preview:
                    awaiting = item.status.value == "awaiting_container"
                    values = (
                        self._status_text(item.status.value),
                        item.tracking,
                        item.carrier_key,
                        item.outbound_at_utc.isoformat(timespec="minutes"),
                        f"{age_minutes} 分",
                        item.container_no,
                        "—" if awaiting else ("已核验" if checked else "待核验"),
                    )
                else:
                    values = (
                        item.tracking,
                        item.carrier_key,
                        item.outbound_at_utc.isoformat(timespec="minutes"),
                        "已核验" if checked else "待核验",
                        self._status_text(item.status.value),
                    )
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setData(Qt.ItemDataRole.UserRole, item.id)
                    if column == 0:
                        cell.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                    table.setItem(row, column, cell)
                if selected_item_id and selected_item_id == item.id:
                    table.selectRow(row)
            label.setText(
                f"第 {page + 1 if page_count else 0} / {page_count} 页 · "
                f"{len(items)} 项"
            )
            previous.setEnabled(page > 0)
            following.setEnabled(page + 1 < page_count)

    def set_import_status(self, text: str) -> None:
        self.preview_status_label.setText(text)

    def set_counts(
        self,
        *,
        counts: Mapping[ItemStatus | str, int] | None = None,
        attempted: int | None = None,
        total: int,
        success: int | None = None,
        duplicates: int | None = None,
        unknown: int | None = None,
    ) -> None:
        if counts is None:
            state_counts = {status: 0 for status in ItemStatus}
            state_counts[ItemStatus.SUCCESS] = int(success or 0)
            state_counts[ItemStatus.ALREADY_IN_BAG] = int(duplicates or 0)
            state_counts[ItemStatus.UNKNOWN] = int(unknown or 0)
            attempted_count = (
                int(attempted)
                if attempted is not None
                else state_counts[ItemStatus.SUCCESS]
                + state_counts[ItemStatus.ALREADY_IN_BAG]
                + state_counts[ItemStatus.UNKNOWN]
            )
        else:
            state_counts = {
                status: int(counts.get(status, counts.get(status.value, 0)))
                for status in ItemStatus
            }
            attempted_count = int(attempted or 0)
        completed = state_counts[ItemStatus.SUCCESS] + state_counts[ItemStatus.ALREADY_IN_BAG]
        self.task_progress_label.setText(f"已确认 {completed} / {total}")
        self.task_attempted_label.setText(f"已尝试 {attempted_count} / {total}")
        self.status_counts_first_label.setText(
            " · ".join(
                (
                    f"待核验 {state_counts[ItemStatus.WAIT_PHYSICAL]}",
                    f"待扫描 {state_counts[ItemStatus.READY]}",
                    f"提交中 {state_counts[ItemStatus.IN_FLIGHT]}",
                    f"新扫描 {state_counts[ItemStatus.SUCCESS]}",
                )
            )
        )
        self.status_counts_second_label.setText(
            " · ".join(
                (
                    f"已在包中 {state_counts[ItemStatus.ALREADY_IN_BAG]}",
                    f"待核对 {state_counts[ItemStatus.UNKNOWN]}",
                    f"已拒绝 {state_counts[ItemStatus.REJECTED]}",
                    f"已取消 {state_counts[ItemStatus.CANCELLED]}",
                )
            )
        )
        self.progress_bar.setValue(int(completed / total * 100) if total else 0)

    def set_import_counts(
        self, *, rows: int, eligible: int, excluded: int, duplicates: int
    ) -> None:
        self.import_rows_label.setText(f"导入 {rows} 行")
        self.import_eligible_label.setText(f"可执行 {eligible}")
        self.import_excluded_label.setText(f"排除 / 重复 {excluded} / {duplicates}")

    def set_import_preview(self, preview: object) -> None:
        self._current_preview = preview
        summary = preview.summary
        excluded_rows = tuple(preview.excluded_rows)
        duplicate_count = sum("duplicate" in row.reason.casefold() for row in excluded_rows)
        self.set_import_counts(
            rows=summary.total_rows,
            eligible=summary.eligible_rows,
            excluded=len(excluded_rows),
            duplicates=duplicate_count,
        )
        self.candidate_search_edit.clear()
        self._awaiting_rows = list(getattr(preview, "awaiting_container", ()))
        self.set_queue_items(preview.candidate_items)
        self._excluded_rows = excluded_rows
        self.preview_export_button.setEnabled(not self._import_busy and not self._is_running)
        preview_text = (
            f"{summary.source_name} · {len(preview.candidate_items)} 项 · "
            f"{len(self._awaiting_rows)} {PENDING_CARRIER_STATE} · "
            f"{len(preview.conflicts)} 冲突"
        )
        self.preview_status_label.setText(preview_text)

    def set_excluded_rows(self, rows: Iterable[object]) -> None:
        self._excluded_rows = tuple(rows)

    def set_review_task_enabled(self, enabled: bool) -> None:
        self.review_task_button.setEnabled(enabled)

    def set_task_preflight(
        self,
        *,
        device_ready: bool,
        task_ready: bool,
        physical_checks_complete: bool,
        unresolved_count: int,
        mode: DeviceMode,
        real_mode_authorized: bool,
    ) -> None:
        self.set_start_conditions(
            device_ready=device_ready,
            task_ready=task_ready,
            physical_checks_complete=physical_checks_complete,
            unresolved_count=unresolved_count,
            mode=mode,
            real_mode_authorized=real_mode_authorized,
        )

    def set_activity_rows(self, rows: Iterable[Iterable[str]]) -> None:
        materialized = [tuple(str(value) for value in row) for row in rows]
        self.activity_table.setRowCount(len(materialized))
        for row, values in enumerate(materialized):
            for column, value in enumerate(values[: self.activity_table.columnCount()]):
                self.activity_table.setItem(row, column, QTableWidgetItem(value))
        self.activity_log_label.setText(f"{len(materialized)} 条")

    def set_current_task(self, name: str) -> None:
        self.workspace_task_label.setText(name or "未载入任务")

    def set_bag_mapping(self, carrier: str, sort_code: str, container_no: str) -> None:
        self.bag_mapping_label.setText(f"{carrier} / {sort_code}")
        self.bag_container_label.setText(f"容器：{container_no}")

    def set_history_rows(self, rows: Iterable[Iterable[str]]) -> None:
        selected_item = self.history_table.item(self.history_table.currentRow(), 0)
        selected_task_id = (
            str(selected_item.data(Qt.ItemDataRole.UserRole)) if selected_item else ""
        )
        materialized = [tuple(str(value) for value in row) for row in rows]
        self.history_table.setRowCount(len(materialized))
        selected_row = -1
        for row, values in enumerate(materialized):
            for column, value in enumerate(values[: self.history_table.columnCount()]):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, value)
                self.history_table.setItem(row, column, cell)
            if values and values[0] == selected_task_id:
                selected_row = row
        if selected_row >= 0:
            self.history_table.selectRow(selected_row)

    def select_history_task(self, task_id: str) -> None:
        for row in range(self.history_table.rowCount()):
            cell = self.history_table.item(row, 0)
            if cell is not None and cell.data(Qt.ItemDataRole.UserRole) == task_id:
                self.history_table.selectRow(row)
                return

    def set_unresolved_rows(self, rows: Iterable[Mapping[str, object]]) -> None:
        records = list(rows)
        self.unresolved_table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = (
                str(record.get("tracking", "")),
                str(record.get("carrier", "")),
                str(record.get("issue", "")),
                str(record.get("logid", "—") or "—"),
                str(record.get("next_step", "核对 WMS")),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, record.get("item_id", ""))
                self.unresolved_table.setItem(row, column, cell)
        self.open_reconciliation_button.setEnabled(False)

    def set_carrier_rows(self, rows: Iterable[Mapping[str, object]]) -> None:
        records = list(rows)
        self.carrier_table.setRowCount(len(records))
        fields = ("carrier_key", "display_name", "aliases", "sort_code", "container_no", "enabled")
        for row, record in enumerate(records):
            for column, field in enumerate(fields):
                value = record.get(field, "")
                if field == "aliases":
                    value = ", ".join(value) if isinstance(value, (tuple, list)) else value
                elif field == "enabled":
                    if record.get("pending"):
                        value = PENDING_CARRIER_STATE
                    else:
                        value = "启用" if value else "停用"
                self.carrier_table.setItem(row, column, QTableWidgetItem(str(value)))

    def set_diagnostics(self, results: Mapping[str, str]) -> None:
        for key, value in results.items():
            if key in self.diagnostic_labels:
                self.diagnostic_labels[key].setText(value)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        if self._is_running:
            answer = QMessageBox.question(
                self,
                "停止扫描并关闭",
                "当前包裹核实后会停止；是否关闭窗口？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
