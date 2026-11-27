"""Wire the desktop UI to isolated simulator/real services."""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from tt_auto_bag_scanner.application.import_service import ImportPreview, ImportService
from tt_auto_bag_scanner.application.startup_recovery import recover_on_startup
from tt_auto_bag_scanner.application.task_service import TaskService
from tt_auto_bag_scanner.domain.models import (
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    ImportMapping,
    ItemStatus,
    QueueItem,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.adb_adapter import AdbDeviceAdapter
from tt_auto_bag_scanner.infrastructure.carrier_config import CarrierConfigService
from tt_auto_bag_scanner.infrastructure.device_manager import DeviceManager
from tt_auto_bag_scanner.infrastructure.excel_parser import ExcelParser, WorkbookValidationError
from tt_auto_bag_scanner.infrastructure.exporter import ReportExporter
from tt_auto_bag_scanner.infrastructure.fake_device import FakeDeviceAdapter
from tt_auto_bag_scanner.infrastructure.paths import ensure_app_data_dir
from tt_auto_bag_scanner.infrastructure.physical_check_service import PhysicalCheckService
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository
from tt_auto_bag_scanner.infrastructure.wms_controller import WmsController
from tt_auto_bag_scanner.ui.main_window import MainWindow

SIMULATED_WAREHOUSE = "SIMULATED-WAREHOUSE"
WMS_PACKAGE = "com.ss.android.pda"


class OperationJob(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
        parent: QObject | None = None,
        *,
        import_job: bool = False,
        device_job: bool = False,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.operation = operation
        self.on_success = on_success
        self.is_import_job = import_job
        self.is_device_job = device_job
        self.cleanup = cleanup

    def run(self) -> None:
        result: object | None = None
        error: Exception | None = None
        try:
            result = self.operation()
        except Exception as exc:
            error = exc
        if self.cleanup is not None:
            try:
                self.cleanup()
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            self.failed.emit(str(error))
            return
        self.succeeded.emit(result)


class AppController(QObject):
    """Application service root; all long I/O stays out of the Qt UI thread."""

    def __init__(
        self,
        window: MainWindow,
        *,
        data_dir: str | Path | None = None,
        poll_interval_ms: int = 400,
    ) -> None:
        super().__init__(window)
        self.window = window
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else ensure_app_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.data_dir / "settings.json"
        self.settings = self._load_settings()
        self.real_mode_authorized = False
        self.mode = DeviceMode.SIMULATED
        self.repositories = {
            mode: TaskRepository(self.data_dir / filename)
            for mode, filename in (
                (DeviceMode.SIMULATED, "simulated.sqlite3"),
                (DeviceMode.REAL, "real.sqlite3"),
            )
        }
        self.task_services = {
            mode: TaskService(self.repositories[mode], actor=self._actor)
            for mode in DeviceMode
        }
        for repository in self.repositories.values():
            recover_on_startup(repository)

        self.import_service = ImportService()
        self.parser = ExcelParser()
        self.carrier_service = CarrierConfigService()
        self.physical_check_service = PhysicalCheckService()
        self.exporter = ReportExporter()
        self.adapters: dict[DeviceMode, FakeDeviceAdapter | AdbDeviceAdapter] = {}
        self.device_managers: dict[DeviceMode, DeviceManager] = {}
        self._make_mode_components(DeviceMode.SIMULATED)
        self._make_mode_components(DeviceMode.REAL)

        self.current_task_id: str | None = None
        self.current_preview: ImportPreview | None = None
        self._last_preview_request: dict[str, object] | None = None
        self.selected_history_task_id: str | None = None
        self.wms_controller: WmsController | None = None
        self.verified_session: VerifiedSession | None = None
        self._controller_invalidated = False
        self._connected = False
        self._wms_app_version = ""
        self._jobs: set[OperationJob] = set()
        self._last_refreshed_task: tuple[DeviceMode, str | None, datetime | None] | None = None
        self._history_filters: dict[str, str] = {
            "query": "",
            "state": "",
            "date_from": "",
            "date_to": "",
        }
        self._history_rows_cache: tuple[tuple[str, ...], ...] | None = None
        self._load_window_settings()
        self.window.set_timezones(sorted(available_timezones()))
        self.window.apply_import_defaults(self._import_defaults())
        self._connect_window()
        self.window.set_mode(DeviceMode.SIMULATED)
        self._select_latest_task()
        self._refresh_visible_data(force=True)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(max(150, poll_interval_ms))
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

    @property
    def repository(self) -> TaskRepository:
        return self.repositories[self.mode]

    @property
    def task_service(self) -> TaskService:
        return self.task_services[self.mode]

    @property
    def adapter(self) -> FakeDeviceAdapter | AdbDeviceAdapter:
        return self.adapters[self.mode]

    @property
    def device_manager(self) -> DeviceManager:
        return self.device_managers[self.mode]

    @property
    def _actor(self) -> str:
        return str(self.settings.get("operator_id", "")).strip() or "local-operator"

    def _load_settings(self) -> dict[str, object]:
        try:
            values = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(values, dict):
            return {}
        values.pop("real_mode_authorized", None)
        values.pop("minimum_age_confirmed", None)
        return values

    def _import_defaults(self) -> dict[str, object]:
        value = self.settings.get("import_defaults")
        return dict(value) if isinstance(value, Mapping) else {}

    def _save_import_defaults(self, mapping: ImportMapping) -> None:
        self.settings["import_defaults"] = {
            "sheet_name": mapping.sheet_name,
            "header_row": mapping.header_row,
            "columns": {
                "tracking": mapping.tracking_column,
                "carrier": mapping.carrier_column,
                "status": mapping.status_column,
                "outbound_at": mapping.outbound_at_column,
                "sort_code": mapping.sort_code_column or "",
            },
            "report_timezone": mapping.report_timezone,
            "threshold_minutes": mapping.threshold_minutes,
        }
        self._write_settings()

    def _write_settings(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temp = self.settings_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.settings_file)

    def _load_window_settings(self) -> None:
        self.window.set_settings({**self.settings, "real_mode_authorized": False})
        if "minimum_age_minutes" in self.settings:
            self.window.threshold_spin.setValue(float(self.settings["minimum_age_minutes"]))

    def _make_mode_components(self, mode: DeviceMode) -> None:
        if mode is DeviceMode.SIMULATED:
            fake_serial = "SIMULATED-PDA"
            adapter = self._make_simulator_adapter()
            if not adapter.devices:
                adapter.devices = (
                    DeviceInfo(
                        serial=fake_serial,
                        model="模拟 PDA",
                        state="device",
                        authorized=True,
                    ),
                )
            manager = DeviceManager(adapter)
            manager.refresh()
            manager.connect(fake_serial)
            self.adapters[mode] = adapter
            self.device_managers[mode] = manager
            return
        adb_path = str(self.settings.get("adb_path") or shutil.which("adb") or "adb")
        adapter = AdbDeviceAdapter(package_name=WMS_PACKAGE, adb_path=adb_path)
        self.adapters[mode] = adapter
        self.device_managers[mode] = DeviceManager(adapter)

    def _make_simulator_adapter(
        self,
        *,
        configs: Sequence[CarrierConfig] | None = None,
        items: Sequence[QueueItem] | None = None,
    ) -> FakeDeviceAdapter:
        selected = tuple(
            configs
            if configs is not None
            else self.repositories[DeviceMode.SIMULATED].list_carrier_configs()
        )
        container_sort: dict[str, str] = {}
        for config in selected:
            container_sort[config.container_no] = config.sort_code
        if items:
            for item in items:
                container_sort[item.container_no] = item.sort_code
        serial = DeviceInfo(
            serial="SIMULATED-PDA",
            model="模拟 PDA",
            state="device",
            authorized=True,
        )
        warehouse = str(self.settings.get("warehouse") or SIMULATED_WAREHOUSE)
        options: dict[str, object] = {
            "devices": (serial,),
            "package_name": WMS_PACKAGE,
            "warehouse": warehouse if container_sort else None,
        }
        if container_sort:
            options["container_sort_map"] = container_sort
        return FakeDeviceAdapter(**options)

    def _new_wms_controller(
        self,
        adapter: FakeDeviceAdapter | AdbDeviceAdapter,
        *,
        warehouse: str,
        real_submission_authorized: bool = False,
    ) -> WmsController:
        timeout = float(self.settings.get("response_timeout_seconds", 15))
        interval = 0.2
        return WmsController(
            adapter,
            warehouse=warehouse,
            real_submission_authorized=real_submission_authorized,
            poll_attempts=max(1, math.ceil(timeout / interval)),
            poll_interval_seconds=interval,
            timeout_seconds=timeout,
        )

    def _connect_window(self) -> None:
        window = self.window
        window.open_report_requested.connect(self._choose_report)
        window.preview_requested.connect(self._build_preview)
        window.preview_export_requested.connect(self._export_preview)
        window.sort_code_overrides_requested.connect(self._rebuild_preview_with_sort_overrides)
        window.review_task_requested.connect(self._review_task)
        window.download_template_requested.connect(self._download_template)
        window.excluded_rows_requested.connect(lambda: None)
        window.device_refresh_requested.connect(self._refresh_devices)
        window.device_connect_requested.connect(self._connect_device)
        window.mode_change_requested.connect(self.set_mode)
        window.real_mode_authorization_requested.connect(self._authorize_real_mode)
        window.physical_check_requested.connect(self._record_physical_check)
        window.cancel_item_requested.connect(self._cancel_unsubmitted_item)
        window.carrier_save_requested.connect(self._save_carrier_config)
        window.container_verify_requested.connect(self._verify_container)
        window.import_source_changed.connect(self._reload_report_headers)
        window.settings_save_requested.connect(self._save_window_settings)
        window.backup_requested.connect(self._backup_database)
        window.restore_requested.connect(self._restore_database)
        window.adb_path_browse_requested.connect(self._choose_adb_path)
        window.diagnostics_requested.connect(self._run_diagnostics)
        window.start_requested.connect(self._start_task)
        window.pause_requested.connect(self._pause_task)
        window.stop_requested.connect(self._stop_task)
        window.export_requested.connect(self._export_report)
        window.history_filter_requested.connect(self._filter_history)
        window.history_task_selected.connect(self._select_history_task)
        window.technical_log_requested.connect(self._show_technical_log)
        window.reconciliation_requested.connect(self._open_reconciliation)

    def _set_status(self, message: str) -> None:
        self.window.set_import_status(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self.window, "操作未完成", message)
        self._set_status(message)

    def _run_job(
        self,
        operation: Callable[[], object],
        on_success: Callable[[object], None],
        *,
        import_job: bool = False,
        device_job: bool = False,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        job = OperationJob(
            operation,
            on_success,
            self,
            import_job=import_job,
            device_job=device_job,
            cleanup=cleanup,
        )
        self._jobs.add(job)
        job.succeeded.connect(self._job_succeeded)
        job.failed.connect(self._job_failed)
        job.finished.connect(self._job_finished)
        job.start()

    def _job_succeeded(self, result: object) -> None:
        job = self.sender()
        if isinstance(job, OperationJob):
            job.on_success(result)

    def _job_failed(self, message: str) -> None:
        job = self.sender()
        if isinstance(job, OperationJob) and job.is_import_job:
            self.window.set_import_busy(False)
        self._show_error(message)
        self._refresh_gates()

    def _has_running_jobs(self) -> bool:
        return any(job.isRunning() for job in self._jobs)

    def _has_pending_device_jobs(self) -> bool:
        return any(job.is_device_job for job in self._jobs)

    def _reject_pending_device_job(self, action: str) -> bool:
        if not self._has_pending_device_jobs():
            return False
        self._show_error(f"设备操作进行中，暂不能{action}")
        return True

    def _job_finished(self) -> None:
        job = self.sender()
        if isinstance(job, OperationJob):
            self._jobs.discard(job)

    def set_mode(self, requested: DeviceMode | str) -> None:
        mode = requested if isinstance(requested, DeviceMode) else DeviceMode(str(requested))
        if mode is self.mode:
            return
        if self._has_active_worker():
            self._set_status("扫描运行中，暂停或停止后再切换模式")
            self.window.set_mode(self.mode)
            return
        if self._has_pending_device_jobs():
            self._set_status("设备操作进行中，完成后再切换模式")
            self.window.set_mode(self.mode)
            return
        previous_controller = self.wms_controller
        if previous_controller is not None:
            previous_controller.real_submission_authorized = False
            previous_controller.invalidate_session()
        self.mode = mode
        self.real_mode_authorized = False
        self._wms_app_version = ""
        self.wms_controller = None
        self.verified_session = None
        self._controller_invalidated = False
        self._connected = mode is DeviceMode.SIMULATED
        self.window.set_mode(mode)
        self.window.device_authorize_check.blockSignals(True)
        self.window.device_authorize_check.setChecked(self.real_mode_authorized)
        self.window.device_authorize_check.blockSignals(False)
        self._select_latest_task()
        self._last_refreshed_task = None
        self._history_rows_cache = None
        self._refresh_visible_data(force=True)

    def _has_active_worker(self) -> bool:
        return any(
            service.active_worker(task.id) is not None
            for mode, service in self.task_services.items()
            for task in self.repositories[mode].list_tasks()
        )

    def _authorize_real_mode(self, requested: bool) -> None:
        if self._has_pending_device_jobs():
            self._set_authorization_check(self.real_mode_authorized)
            self._show_error("设备操作进行中，暂不能更改实机授权")
            return
        if not requested:
            self.real_mode_authorized = False
            if self.wms_controller is not None:
                self.wms_controller.real_submission_authorized = False
            if self.current_task_id and self.task_service.active_worker(self.current_task_id):
                self.task_service.stop_task(self.current_task_id)
            self._refresh_gates()
            return
        if self.mode is not DeviceMode.REAL:
            self._set_authorization_check(False)
            self._show_error("切换到实机模式后再授权")
            return
        if not str(self.settings.get("operator_id", "")).strip():
            self._set_authorization_check(False)
            self._show_error("请先填写操作员 ID")
            return
        answer = QMessageBox.question(
            self.window,
            "实机授权",
            "确认本站已获实机作业授权？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self.real_mode_authorized = answer == QMessageBox.StandardButton.Yes
        self._set_authorization_check(self.real_mode_authorized)
        if self.wms_controller is not None:
            self.wms_controller.real_submission_authorized = self.real_mode_authorized
        self._refresh_gates()

    def _set_authorization_check(self, checked: bool) -> None:
        self.window.device_authorize_check.blockSignals(True)
        self.window.device_authorize_check.setChecked(checked)
        self.window.device_authorize_check.blockSignals(False)

    def _save_window_settings(self, incoming: Mapping[str, object]) -> None:
        if self._reject_pending_device_job("修改设置"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能修改设置")
            return
        values = dict(incoming)
        values.pop("real_mode_authorized", None)
        values.pop("mode", None)
        values.pop("minimum_age_confirmed", None)
        warehouse_changed = values.get("warehouse") != self.settings.get("warehouse")
        adb_changed = values.get("adb_path") != self.settings.get("adb_path")
        timeout_changed = (
            values.get("response_timeout_seconds")
            != self.settings.get("response_timeout_seconds")
        )
        self.settings.update(values)
        self._write_settings()
        if adb_changed:
            self._make_mode_components(DeviceMode.REAL)
            if self.mode is DeviceMode.REAL:
                self._connected = False
        if warehouse_changed and self.mode is DeviceMode.SIMULATED:
            self._make_mode_components(DeviceMode.SIMULATED)
        if warehouse_changed or timeout_changed or (adb_changed and self.mode is DeviceMode.REAL):
            self.wms_controller = None
            self.verified_session = None
            self._controller_invalidated = True
            self.window.set_device_status(
                connected=self._connected,
                session_verified=False,
            )
        self.window.threshold_spin.setValue(
            float(self.settings.get("minimum_age_minutes", self.window.threshold_spin.value()))
        )
        self.window.confirm_threshold_check.setChecked(False)
        self._set_status("设置已保存")
        self._refresh_gates()

    def _choose_adb_path(self) -> None:
        if self._reject_pending_device_job("更改 ADB 路径"):
            return
        current = str(self.settings.get("adb_path", ""))
        path, _ = QFileDialog.getOpenFileName(self.window, "选择 ADB", current, "adb.exe (adb.exe)")
        if path:
            self.window.adb_path_edit.setText(path)

    def _refresh_devices(self) -> None:
        if self._reject_pending_device_job("刷新设备"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能刷新设备")
            return
        if self.mode is DeviceMode.SIMULATED:
            devices = self.device_manager.refresh()
            self.window.set_devices(devices)
            self._connected = self.device_manager.connection_state.value == "connected"
            self.window.set_device_status(
                connected=self._connected,
                app_version="模拟 WMS",
                session_verified=self._verified_session_valid(),
                warehouse=self.verified_session.warehouse if self.verified_session else "",
                container_no=(
                    self.verified_session.container_no if self.verified_session else ""
                ),
            )
            self._refresh_gates()
            return
        self._run_job(self.device_manager.refresh, self._devices_refreshed, device_job=True)

    def _devices_refreshed(self, result: object) -> None:
        devices = tuple(result)  # type: ignore[arg-type]
        self.window.set_devices(devices)
        self._connected = self.device_manager.connection_state.value == "connected"
        if not self._connected:
            self._wms_app_version = ""
            self.verified_session = None
            if self.wms_controller is not None:
                self.wms_controller.invalidate_session()
            self.window.set_device_status(connected=False, session_verified=False)
        self._set_status(f"发现 {len(devices)} 台设备")
        self._refresh_gates()

    def _connect_device(self, serial: str) -> None:
        if self._reject_pending_device_job("连接设备"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能重新连接设备")
            return
        if not serial.strip():
            self._show_error("请选择设备")
            return

        def connect() -> DeviceInfo:
            device = self.device_manager.connect(serial)
            self.device_manager.launch_app(WMS_PACKAGE)
            return device

        self._run_job(connect, self._device_connected, device_job=True)

    def _device_connected(self, result: object) -> None:
        self._connected = True
        self._wms_app_version = str(getattr(result, "app_version", "") or "").strip()
        self.wms_controller = None
        self.verified_session = None
        self._controller_invalidated = False
        self.window.set_device_status(
            connected=True,
            app_version=self._current_wms_version(),
            session_verified=False,
        )
        self._set_status("请在 PDA 手动登录")
        self._refresh_gates()

    def _choose_report(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window, "选择出库报表", "", "Excel 工作簿 (*.xlsx)"
        )
        if not path:
            return
        self.window.set_report_path(path)
        self.window.set_import_sheets(())
        self.window.set_import_headers(())
        self._set_status("读取报表列")
        self.window.set_import_busy(True)
        defaults = self._import_defaults()
        self._run_job(
            lambda: self._inspect_report(path, defaults),
            lambda result: self._report_headers_loaded(path, result),
            import_job=True,
        )

    def _inspect_report(
        self,
        path: str,
        defaults: Mapping[str, object] | None = None,
    ) -> tuple[tuple[str, ...], str, int, tuple[str, ...]]:
        sheets = self.parser.list_sheet_names(path)
        if not sheets:
            raise ValueError("工作簿没有可用工作表")
        saved = defaults or {}
        preferred_sheet = saved.get("sheet_name")
        sheet_name = preferred_sheet if isinstance(preferred_sheet, str) else ""
        if sheet_name not in sheets:
            sheet_name = sheets[0]
            header_row = 1
        else:
            try:
                header_row = int(saved.get("header_row", 1))
            except (TypeError, ValueError):
                header_row = 1
            if header_row < 1:
                header_row = 1
        try:
            headers = self.parser.read_headers(
                path,
                sheet_name=sheet_name,
                header_row=header_row,
            )
        except WorkbookValidationError:
            if sheet_name == sheets[0] and header_row == 1:
                raise
            sheet_name = sheets[0]
            header_row = 1
            headers = self.parser.read_headers(
                path,
                sheet_name=sheet_name,
                header_row=header_row,
            )
        return sheets, sheet_name, header_row, headers

    def _report_headers_loaded(self, path: str, result: object) -> None:
        if self.window.report_path_edit.text() != path:
            self.window.set_import_busy(False)
            return
        sheets, sheet_name, header_row, headers = result  # type: ignore[misc]
        self.window.set_import_sheets(sheets, selected=sheet_name)
        self.window.header_row_spin.blockSignals(True)
        self.window.header_row_spin.setValue(header_row)
        self.window.header_row_spin.blockSignals(False)
        selected_columns = self._import_defaults().get("columns")
        self.window.set_import_headers(
            headers,
            selected_columns=(
                selected_columns if isinstance(selected_columns, Mapping) else None
            ),
        )
        self.window.set_import_busy(False)
        self._set_status(f"{len(sheets)} 个工作表 · {len(headers)} 列")

    def _reload_report_headers(self, sheet_name: str, header_row: int) -> None:
        path = self.window.report_path_edit.text()
        if not path or not sheet_name:
            return
        self._set_status("读取表头")
        self.window.set_import_busy(True)
        self._run_job(
            lambda: self.parser.read_headers(
                path,
                sheet_name=sheet_name,
                header_row=header_row,
            ),
            lambda result: self._headers_loaded(path, sheet_name, header_row, result),
            import_job=True,
        )

    def _headers_loaded(
        self,
        path: str,
        sheet_name: str,
        header_row: int,
        result: object,
    ) -> None:
        if (
            self.window.report_path_edit.text() != path
            or self.window.sheet_combo.currentData() != sheet_name
            or int(self.window.header_row_spin.value()) != header_row
        ):
            self.window.set_import_busy(False)
            return
        headers = tuple(result)  # type: ignore[arg-type]
        selected_columns = self._import_defaults().get("columns")
        self.window.set_import_headers(
            headers,
            selected_columns=(
                selected_columns if isinstance(selected_columns, Mapping) else None
            ),
        )
        self.window.set_import_busy(False)
        self._set_status(f"{len(headers)} 列")

    def _build_preview(self, request: Mapping[str, object]) -> None:
        try:
            path = str(request["path"])
            mapping = ImportMapping(
                tracking_column=str(request["tracking"]),
                carrier_column=str(request["carrier"]),
                status_column=str(request["status"]),
                outbound_at_column=str(request["outbound_at"]),
                report_timezone=str(request["report_timezone"]),
                threshold_minutes=int(request["threshold_minutes"]),
                sort_code_column=(
                    str(request["sort_code"]) if request.get("sort_code") else None
                ),
                sheet_name=str(request["sheet_name"]),
                header_row=int(request["header_row"]),
                sort_code_overrides={
                    int(row_number): str(sort_code)
                    for row_number, sort_code in dict(
                        request.get("sort_code_overrides", {}) or {}
                    ).items()
                },
            )
            if not bool(request.get("threshold_confirmed")):
                raise ValueError("请确认筛选阈值")
            if not mapping.report_timezone:
                raise ValueError("请选择报表时区")
            if not path or not mapping.sheet_name:
                raise ValueError("请选择 XLSX 报表")
        except (KeyError, TypeError, ValueError) as exc:
            self._show_error(str(exc))
            return
        task_id = str(uuid4())
        mode = self.mode
        if mode is DeviceMode.REAL and not self.real_mode_authorized:
            self._show_error("生成实机任务前必须完成实机授权")
            return
        self._save_import_defaults(mapping)
        configs = self.repository.list_carrier_configs()
        self._last_preview_request = dict(request)
        self._last_preview_request["sort_code_overrides"] = dict(mapping.sort_code_overrides)
        repository = self.repository
        actor = self._actor
        authorized = self.real_mode_authorized
        self.window.set_import_busy(True)
        self._set_status("正在生成预览")
        self._run_job(
            lambda: self.import_service.build_preview(
                path,
                mapping,
                configs,
                datetime.now(UTC),
                task_id=task_id,
                sheet_name=mapping.sheet_name,
                header_row=mapping.header_row,
            ),
            lambda preview: self._preview_built(
                preview,
                task_id,
                repository=repository,
                mode=mode,
                authorized=authorized,
                configs=configs,
                actor=actor,
            ),
            import_job=True,
        )

    def _preview_built(
        self,
        result: object,
        task_id: str,
        *,
        repository: TaskRepository,
        mode: DeviceMode,
        authorized: bool,
        configs: Sequence[CarrierConfig],
        actor: str,
    ) -> None:
        preview: ImportPreview = result  # type: ignore[assignment]
        if repository.has_import_hash(preview.summary.source_hash):
            answer = QMessageBox.question(
                self.window,
                "报表重复导入",
                "此文件已有导入记录。已核对历史任务后，仍要新建任务吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.window.set_import_busy(False)
                self._set_status("已取消重复导入")
                return
        self._set_status("正在保存导入快照")
        self._run_job(
            lambda: self._persist_preview(
                preview,
                task_id,
                repository=repository,
                mode=mode,
                authorized=authorized,
                configs=configs,
                actor=actor,
            ),
            lambda saved: self._preview_saved(preview, task_id, mode, saved),
            import_job=True,
            cleanup=repository.close,
        )

    def _rebuild_preview_with_sort_overrides(self, overrides: object) -> None:
        if self._last_preview_request is None:
            self._show_error("先选择报表并生成预览")
            return
        if not isinstance(overrides, Mapping):
            self._show_error("排序码选择无效")
            return
        request = dict(self._last_preview_request)
        request["sort_code_overrides"] = dict(overrides)
        self._build_preview(request)

    def _persist_preview(
        self,
        preview: ImportPreview,
        task_id: str,
        *,
        repository: TaskRepository,
        mode: DeviceMode,
        authorized: bool,
        configs: Sequence[CarrierConfig],
        actor: str,
    ) -> str:
        now = datetime.now(UTC)
        import_id = str(uuid4())
        reason_by_row: dict[int, str] = {
            item.source_row_number: item.reason for item in preview.excluded_rows
        }
        mapping = preview.summary.mapping
        try:
            timezone = ZoneInfo(mapping.report_timezone)
        except ZoneInfoNotFoundError:
            timezone = None
        raw_rows = []
        for source in preview.source_rows:
            raw = {key: self._json_value(value) for key, value in source.values.items()}
            outbound_value = source.values.get(mapping.outbound_at_column)
            outbound_utc = None
            if timezone is not None:
                try:
                    outbound_utc, _issue = self.import_service._to_utc(
                        outbound_value,
                        timezone,
                    )
                except (TypeError, ValueError, OverflowError):
                    outbound_utc = None
            raw_rows.append(
                {
                    "row_number": source.row_number,
                    "raw": raw,
                    "tracking": str(source.values.get(mapping.tracking_column, "") or ""),
                    "carrier": str(source.values.get(mapping.carrier_column, "") or ""),
                    "status_raw": str(source.values.get(mapping.status_column, "") or ""),
                    "outbound_utc": outbound_utc,
                    "exclude_reason": reason_by_row.get(source.row_number),
                }
            )
        repository.save_import_snapshot(
            import_id=import_id,
            file_name=preview.summary.source_name,
            source_hash=preview.summary.source_hash,
            sheet_name=preview.sheet_name,
            mapping=asdict(mapping),
            timezone=mapping.report_timezone,
            rows=raw_rows,
            imported_at_utc=preview.summary.created_at_utc,
            row_count=len(raw_rows),
        )
        if preview.candidate_items:
            counts = TaskCounts.from_statuses([item.status for item in preview.candidate_items])
            task = TaskSnapshot(
                id=task_id,
                name=preview.summary.source_name,
                state=TaskState.DRAFT,
                item_ids=tuple(item.id for item in preview.candidate_items),
                counts=counts,
                created_at_utc=now,
                updated_at_utc=now,
                device_mode=mode,
                real_mode_authorized=authorized,
                source_hash=preview.summary.source_hash,
            )
            config_snapshot = [
                {
                    "carrier_key": config.carrier_key,
                    "sort_code": config.sort_code,
                    "container_no": config.container_no,
                    "version": config.version,
                }
                for config in configs
            ]
            repository.create_task(
                task,
                preview.candidate_items,
                import_id=import_id,
                config_snapshot={"carrier_configs": config_snapshot},
                actor=actor,
            )
        return import_id

    def _preview_saved(
        self,
        preview: ImportPreview,
        task_id: str,
        mode: DeviceMode,
        _import_id: object,
    ) -> None:
        self.window.set_import_busy(False)
        if mode is not self.mode:
            self._set_status("导入已保存到原运行模式的本地记录")
            return
        self.current_task_id = task_id if preview.candidate_items else None
        self.selected_history_task_id = self.current_task_id
        self.current_preview = preview
        self.window.set_import_preview(preview)
        self.window.set_current_task(
            preview.summary.source_name if self.current_task_id else "无可执行项"
        )
        self.window.navigate_to("导入报表")
        self._last_refreshed_task = None
        self._history_rows_cache = None
        self._refresh_visible_data(force=True)
        self._set_status("预览已保存")

    @staticmethod
    def _json_value(value: object) -> object:
        if isinstance(value, datetime):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _review_task(self) -> None:
        if self._reject_pending_device_job("审核任务"):
            return
        if not self.current_task_id:
            self._show_error("没有可审核任务")
            return
        items = self.repository.list_items(self.current_task_id)
        missing = sum(item.status is ItemStatus.WAIT_PHYSICAL for item in items)
        if missing:
            self._set_status(f"{missing} 项待实物核验")
            return
        task = self.repository.get_task(self.current_task_id)
        if task.state is TaskState.DRAFT:
            self.repository.transition_task(
                self.current_task_id,
                TaskState.READY,
                actor=self._actor,
                reason="operator reviewed the verified queue",
            )
        if self.mode is DeviceMode.SIMULATED and items:
            try:
                self._prepare_simulated_session(items[0], items)
            except Exception as exc:
                self._show_error(f"模拟预检失败：{exc}")
                return
        self.window.navigate_to("扫描工作区")
        self._refresh_visible_data()

    def _prepare_simulated_session(
        self, item: QueueItem, items: Sequence[QueueItem]
    ) -> VerifiedSession:
        adapter = self._make_simulator_adapter(items=items)
        self.adapters[DeviceMode.SIMULATED] = adapter
        manager = DeviceManager(adapter)
        manager.refresh()
        manager.connect("SIMULATED-PDA")
        self.device_managers[DeviceMode.SIMULATED] = manager
        controller = self._new_wms_controller(
            adapter,
            warehouse=str(self.settings.get("warehouse") or SIMULATED_WAREHOUSE),
        )
        session = controller.open_bag(self._config_for_item(item))
        self.wms_controller = controller
        self.verified_session = session
        self._connected = True
        self._controller_invalidated = False
        return session

    def _save_carrier_config(self, data: Mapping[str, object]) -> None:
        if self._reject_pending_device_job("修改容器配置"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能修改容器配置")
            return
        try:
            carrier_key = str(data.get("carrier_key", "")).strip()
            sort_code = str(data.get("sort_code", "")).strip()
            if not carrier_key or not sort_code:
                raise ValueError("承运商键和分拣码必填")
            configs = list(self.repository.list_carrier_configs())
            previous = next(
                (
                    config
                    for config in configs
                    if config.carrier_key.casefold() == carrier_key.casefold()
                    and config.sort_code.casefold() == sort_code.casefold()
                ),
                None,
            )
            config = CarrierConfig(
                carrier_key=carrier_key,
                display_name=str(data.get("display_name", "")).strip() or carrier_key,
                aliases=tuple(data.get("aliases", ())),
                sort_code=sort_code,
                container_no=str(data.get("container_no", "")).strip(),
                enabled=bool(data.get("enabled", True)),
                version=(previous.version + 1) if previous else 1,
                updated_at_utc=datetime.now(UTC),
            )
            updated = [
                existing
                for existing in configs
                if not (
                    existing.carrier_key.casefold() == carrier_key.casefold()
                    and existing.sort_code.casefold() == sort_code.casefold()
                )
            ]
            updated.append(config)
            self.carrier_service.validate(updated)
            self.repository.save_carrier_configs(updated, actor=self._actor)
        except Exception as exc:
            self._show_error(f"配置未保存：{exc}")
            return
        self.window.set_carrier_rows(self._carrier_rows())
        self._controller_invalidated = True
        self.verified_session = None
        if self.mode is DeviceMode.SIMULATED:
            self._make_mode_components(DeviceMode.SIMULATED)
        self._set_status("承运商配置已保存")
        self._refresh_gates()

    def _carrier_rows(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "carrier_key": item.carrier_key,
                "display_name": item.display_name,
                "aliases": item.aliases,
                "sort_code": item.sort_code,
                "container_no": item.container_no,
                "enabled": item.enabled,
            }
            for item in self.repository.list_carrier_configs()
        )

    def _verify_container(self, carrier_key: str) -> None:
        if self._reject_pending_device_job("核验或切换容器"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能切换容器")
            return
        if self.current_task_id and self.repository.has_unresolved(self.current_task_id):
            self._show_error("未解决的提交结果禁止切换容器")
            return
        carrier_key = carrier_key.strip()
        if not carrier_key:
            self._show_error("选择承运商配置")
            return
        configs = self.repository.list_carrier_configs()
        config = next(
            (
                current
                for current in configs
                if current.carrier_key.casefold() == carrier_key.casefold()
                and current.sort_code == self.window.sort_code_edit.text().strip()
                and current.container_no == self.window.container_edit.text().strip()
            ),
            None,
        )
        if config is None:
            self._show_error("当前承运商、分拣码与容器尚未保存")
            return
        if self.mode is DeviceMode.REAL and not self.real_mode_authorized:
            self._show_error("实机操作未授权")
            return
        if self.mode is DeviceMode.REAL and not self._connected:
            self._show_error("请先连接 PDA")
            return
        warehouse = str(self.settings.get("warehouse", "")).strip()
        if self.mode is DeviceMode.SIMULATED:
            warehouse = warehouse or SIMULATED_WAREHOUSE
            adapter = self._make_simulator_adapter(configs=configs)
            self.adapters[DeviceMode.SIMULATED] = adapter
            manager = DeviceManager(adapter)
            manager.refresh()
            manager.connect("SIMULATED-PDA")
            self.device_managers[DeviceMode.SIMULATED] = manager
            controller = self._new_wms_controller(adapter, warehouse=warehouse)
            self.wms_controller = controller
            self.verified_session = None
            self._controller_invalidated = False
            self._run_job(
                lambda: controller.verify_current_or_open_bag(config),
                lambda session: self._container_verified(
                    controller,
                    session,
                    config,
                    expected_mode=DeviceMode.SIMULATED,
                    expected_adapter=adapter,
                ),
                device_job=True,
            )
            return
        if not warehouse:
            self._show_error("请在设置中填写 WMS 仓库标识")
            return
        if self._controller_invalidated or self.wms_controller is None:
            self.wms_controller = self._new_wms_controller(
                self.adapter,
                warehouse=warehouse,
                real_submission_authorized=self.real_mode_authorized,
            )
            self._controller_invalidated = False
        controller = self.wms_controller
        mode = self.mode
        adapter = self.adapter
        current_session = self.verified_session
        unresolved_submissions = int(
            self.current_task_id is not None
            and self.repository.has_unresolved(self.current_task_id)
        )

        def verify() -> VerifiedSession:
            if current_session is not None and (
                current_session.container_no != config.container_no
                or current_session.sort_code != config.sort_code
            ):
                return controller.switch_bag(
                    config,
                    unresolved_submissions=unresolved_submissions,
                )
            return controller.verify_current_or_open_bag(config)

        self._run_job(
            verify,
            lambda session: self._container_verified(
                controller,
                session,
                config,
                expected_mode=mode,
                expected_adapter=adapter,
            ),
            device_job=True,
        )

    def _container_verified(
        self,
        controller: WmsController,
        session: object,
        config: CarrierConfig,
        *,
        expected_mode: DeviceMode,
        expected_adapter: FakeDeviceAdapter | AdbDeviceAdapter,
    ) -> None:
        if (
            self.mode is not expected_mode
            or self.adapters.get(expected_mode) is not expected_adapter
            or controller.adapter is not expected_adapter
            or self.wms_controller is not controller
        ):
            controller.real_submission_authorized = False
            controller.invalidate_session()
            return
        self.wms_controller = controller
        self.verified_session = session  # type: ignore[assignment]
        self._connected = True
        self._controller_invalidated = False
        self.window.set_bag_mapping(config.carrier_key, config.sort_code, config.container_no)
        self.window.set_device_status(
            connected=True,
            warehouse=session.warehouse,
            container_no=session.container_no,
            app_version=self._current_wms_version(),
            session_verified=True,
        )
        self._set_status("WMS 容器与分拣码已验证")
        self._refresh_gates()

    def _record_physical_check(self, item_id: str, operator: str) -> None:
        if not self.current_task_id:
            self._show_error("没有活动任务")
            return
        try:
            item = self.repository.get_item(item_id)
            evidence_ref = f"operator-attestation:{uuid4()}"
            check = self.physical_check_service.record_manual(
                item,
                operator=operator,
                evidence_ref=evidence_ref,
                checked_at_utc=datetime.now(UTC),
            )
            self.repository.record_physical_check(check, actor=operator)
        except Exception as exc:
            self._show_error(f"实物核验未保存：{exc}")
            return
        self._set_status("实物核验已记录")
        self._refresh_visible_data()

    def _cancel_unsubmitted_item(self, item_id: str) -> None:
        if not self.current_task_id:
            self._show_error("没有活动任务")
            return
        try:
            item = self.repository.get_item(item_id)
            if item.task_id != self.current_task_id:
                raise RepositoryError("所选包裹不属于当前任务")
            self.task_service.cancel_unsubmitted_item(item_id)
        except Exception as exc:
            self._show_error(f"包裹取消失败：{exc}")
            return
        self._set_status("包裹已取消")
        self._refresh_visible_data(force=True)

    def _config_for_item(self, item: QueueItem) -> CarrierConfig:
        current = next(
            (
                config
                for config in self.repository.list_carrier_configs()
                if config.carrier_key.casefold() == item.carrier_key.casefold()
                and config.sort_code.casefold() == item.sort_code.casefold()
                and config.container_no == item.container_no
                and config.enabled
            ),
            None,
        )
        if current is None:
            raise ValueError(
                "carrier configuration no longer matches this task; create a new task"
            )
        return current

    def _start_task(self) -> None:
        if self._reject_pending_device_job("启动扫描"):
            return
        task_id = self.current_task_id
        if not task_id:
            self._show_error("没有可运行任务")
            return
        task = self.repository.get_task(task_id)
        if task.device_mode is not self.mode:
            self._show_error("任务模式与当前模式不一致，请重新导入")
            return
        if self._has_active_worker():
            self._show_error("已有扫描任务占用设备")
            return
        if self.mode is DeviceMode.REAL and not self.real_mode_authorized:
            self._show_error("实机操作未授权")
            return
        if self.wms_controller is None or self.verified_session is None:
            self._show_error("先在容器设置中验证 PDA")
            return
        if not self._verified_session_valid():
            self._show_error("PDA 会话已失效，请重新验证")
            return
        controller = self.wms_controller
        repository = self.repository

        def session_provider(item: QueueItem) -> VerifiedSession:
            current = self.verified_session
            target = self._config_for_item(item)
            if current is not None and (
                current.container_no == item.container_no
                and current.sort_code == item.sort_code
                and current.warehouse == str(
                    self.settings.get("warehouse") or SIMULATED_WAREHOUSE
                )
            ):
                return current
            if current is not None:
                session = controller.switch_bag(
                    target,
                    unresolved_submissions=int(repository.has_unresolved(task_id)),
                )
            else:
                session = controller.open_bag(target)
            self.verified_session = session
            return session

        try:
            self.task_service.start_task(
                task_id,
                controller,
                session_provider,
                real_mode_authorized=self.real_mode_authorized,
            )
        except Exception as exc:
            self._show_error(f"任务未启动：{exc}")
            self._refresh_visible_data()
            return
        self.window.set_running(True)
        self._set_status("任务运行中")
        self._poll()

    def _pause_task(self) -> None:
        if not self.current_task_id:
            return
        try:
            self.task_service.pause_task(self.current_task_id)
        except Exception as exc:
            self._show_error(f"暂停失败：{exc}")
        self._poll()

    def _stop_task(self) -> None:
        if not self.current_task_id:
            return
        try:
            self.task_service.stop_task(self.current_task_id)
        except Exception as exc:
            self._show_error(f"停止失败：{exc}")
        self._poll()

    def _select_latest_task(self) -> None:
        tasks = self.repository.list_task_summaries()
        self.current_task_id = str(tasks[-1]["id"]) if tasks else None
        self.selected_history_task_id = self.current_task_id
        self._last_refreshed_task = None

    def _refresh_visible_data(self, *, force: bool = False) -> None:
        current_updated_at: datetime | None = None
        if self.current_task_id:
            try:
                current_updated_at = self.repository.get_task(self.current_task_id).updated_at_utc
            except RepositoryError:
                self.current_task_id = None
        marker = (self.mode, self.current_task_id, current_updated_at)
        if not force and marker == self._last_refreshed_task:
            return
        self._last_refreshed_task = marker
        self.window.set_carrier_rows(self._carrier_rows())
        if self.current_task_id:
            try:
                items = self.repository.list_items(self.current_task_id)
                self.window.set_queue_items(items)
                task = self.repository.get_task(self.current_task_id)
                self.window.set_current_task(task.name)
                stats = self.repository.statistics(self.current_task_id)
                self.window.set_counts(
                    counts=stats,
                    attempted=stats.get("attempts", 0),
                    total=stats.get("total", len(items)),
                )
                self.window.set_activity_rows(
                    self._task_activity_rows(self.current_task_id)[-100:]
                )
            except RepositoryError as exc:
                self._show_error(str(exc))
        else:
            self.window.set_queue_items(())
            self.window.set_current_task("")
            self.window.set_counts(success=0, duplicates=0, unknown=0, total=0)
            self.window.set_activity_rows(())
            self.window.set_unresolved_rows(())
        self._refresh_history()
        if self.selected_history_task_id:
            self._refresh_history_detail(self.selected_history_task_id)
        else:
            self.window.set_unresolved_rows(())
        self._refresh_gates()

    def _history_row(self, task: TaskSnapshot | Mapping[str, object]) -> tuple[str, ...]:
        if isinstance(task, Mapping):
            task_id = str(task["id"])
            created_at = task["created_at_utc"]
            counts = task["counts"]
            state = task["state"]
        else:
            task_id = task.id
            created_at = task.created_at_utc
            counts = task.counts
            state = task.state
        if not isinstance(counts, TaskCounts) or not isinstance(created_at, datetime):
            raise TypeError("task history summary has invalid field types")
        if not isinstance(state, TaskState):
            raise TypeError("task history state has an invalid type")
        processed = counts.confirmed_complete
        return (
            task_id,
            created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
            f"{processed} / {counts.total}",
            str(counts.by_status[ItemStatus.SUCCESS]),
            str(counts.by_status[ItemStatus.ALREADY_IN_BAG]),
            str(self.repository.unresolved_count(task_id)),
            state.value,
        )

    def _item_reconciliation_state(self, item: QueueItem) -> tuple[bool, str, dict[str, object]]:
        if item.status not in {
            ItemStatus.UNKNOWN,
            ItemStatus.IN_FLIGHT,
            ItemStatus.ALREADY_IN_BAG,
        }:
            return False, "", {}
        attempt = self.repository.latest_attempt(item.id) or {}
        if item.status in {ItemStatus.UNKNOWN, ItemStatus.IN_FLIGHT}:
            return True, str(attempt.get("error_code") or item.status.value), attempt
        recovery_status = str(attempt.get("recovery_status") or "")
        if item.status is ItemStatus.ALREADY_IN_BAG and recovery_status in {
            "pending",
            "in_flight",
            "failed",
            "unknown",
        }:
            return True, f"duplicate_recovery_{recovery_status}", attempt
        return False, "", attempt

    def _set_unresolved_rows(self, task_id: str) -> None:
        rows: list[dict[str, object]] = []
        for item in self.repository.list_items(task_id):
            unresolved, issue, attempt = self._item_reconciliation_state(item)
            if not unresolved:
                continue
            evidence = self._attempt_evidence(attempt)
            rows.append(
                {
                    "item_id": item.id,
                    "tracking": item.tracking,
                    "carrier": item.carrier_key,
                    "issue": issue,
                    "logid": evidence.get("logid") or "未返回",
                    "next_step": "核对 WMS",
                }
            )
        self.window.set_unresolved_rows(rows)

    def _refresh_history(self) -> None:
        tasks = self.repository.list_task_summaries()
        query = self._history_filters["query"]
        state = self._history_filters["state"]
        date_from = self._history_filters["date_from"]
        date_to = self._history_filters["date_to"]
        tracking_task_ids = (
            self.repository.task_ids_matching_tracking(query) if query else frozenset()
        )
        selected = []
        for task in tasks:
            task_id = str(task["id"])
            created_at = task["created_at_utc"]
            task_name = str(task["name"])
            task_state = task["state"]
            if not isinstance(created_at, datetime) or not isinstance(task_state, TaskState):
                continue
            created = created_at.astimezone(UTC).strftime("%Y-%m-%d")
            if (
                query
                and query not in task_id.casefold()
                and query not in task_name.casefold()
                and task_id not in tracking_task_ids
            ):
                continue
            if state and task_state.value != state:
                continue
            if date_from and created < date_from:
                continue
            if date_to and created > date_to:
                continue
            selected.append(self._history_row(task))
        rows = tuple(selected)
        if rows != self._history_rows_cache:
            self.window.set_history_rows(rows)
            self._history_rows_cache = rows
        if self.selected_history_task_id:
            self.window.select_history_task(self.selected_history_task_id)

    def _refresh_history_detail(self, task_id: str) -> None:
        try:
            self.repository.get_task(task_id)
            self._set_unresolved_rows(task_id)
        except RepositoryError:
            self.window.set_unresolved_rows(())

    def _refresh_gates(self) -> None:
        items = self.repository.list_items(self.current_task_id) if self.current_task_id else ()
        mode_is_simulation = self.mode is DeviceMode.SIMULATED
        self.window.set_task_preflight(
            device_ready=(
                self._device_ready() and (mode_is_simulation or self.real_mode_authorized)
            ),
            task_ready=self._task_ready(),
            physical_checks_complete=all(
                item.status is not ItemStatus.WAIT_PHYSICAL for item in items
            ) and bool(items),
            unresolved_count=self._unresolved_count(),
            mode=self.mode,
            real_mode_authorized=self.real_mode_authorized,
        )

    def _device_ready(self) -> bool:
        if self.mode is DeviceMode.SIMULATED:
            return self._connected
        return self._connected and self._verified_session_valid()

    def _task_ready(self) -> bool:
        if not self.current_task_id:
            return False
        try:
            task = self.repository.get_task(self.current_task_id)
        except RepositoryError:
            return False
        return task.state in {
            TaskState.READY,
            TaskState.PAUSED,
            TaskState.STOPPED,
        }

    def _physical_complete(self) -> bool:
        if not self.current_task_id:
            return False
        items = self.repository.list_items(self.current_task_id)
        return bool(items) and all(
            item.status is not ItemStatus.WAIT_PHYSICAL for item in items
        )

    def _unresolved_count(self) -> int:
        if not self.current_task_id:
            return 0
        return self.repository.unresolved_count(self.current_task_id)

    @staticmethod
    def _attempt_evidence(attempt: Mapping[str, object]) -> Mapping[str, object]:
        value = attempt.get("after")
        if isinstance(value, Mapping):
            return value
        raw = attempt.get("after_json")
        if not isinstance(raw, str):
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _verified_session_valid(self) -> bool:
        return bool(self.verified_session and self.verified_session.valid)

    def _poll(self) -> None:
        if not self.current_task_id:
            return
        try:
            task = self.repository.get_task(self.current_task_id)
        except RepositoryError:
            return
        marker = (self.mode, task.id, task.updated_at_utc)
        if marker == self._last_refreshed_task:
            return
        self.window.set_running(task.state in {TaskState.RUNNING, TaskState.PAUSING})
        if task.state is TaskState.BLOCKED:
            self._controller_invalidated = True
            self.verified_session = None
            if self.wms_controller is not None:
                self.wms_controller.invalidate_session()
                self.wms_controller.real_submission_authorized = False
            self.window.set_device_status(connected=self._connected, session_verified=False)
            if not self.repository.has_unresolved(task.id):
                try:
                    self.repository.transition_task(
                        task.id,
                        TaskState.PAUSED,
                        actor=self._actor,
                        reason="worker stopped with no unresolved WMS submissions",
                    )
                    self._set_status("任务已暂停；修正后可重新预检")
                except RepositoryError:
                    self._set_status("任务已阻止；请查看任务记录")
            else:
                self._set_status("任务已阻止；请先核对 WMS")
        elif task.state is TaskState.STOPPED:
            self._controller_invalidated = True
            self.verified_session = None
            self.real_mode_authorized = False
            self._set_authorization_check(False)
            if self.wms_controller is not None:
                self.wms_controller.real_submission_authorized = False
                self.wms_controller.invalidate_session()
            self.window.set_device_status(connected=self._connected, session_verified=False)
            self._set_status("任务已停止；恢复前请重新授权并核验容器")
        self._refresh_visible_data(force=True)

    def _filter_history(self, filters: Mapping[str, object]) -> None:
        self._history_filters = {
            "query": str(filters.get("query", "")).casefold(),
            "state": str(filters.get("state", "")),
            "date_from": str(filters.get("date_from", "")),
            "date_to": str(filters.get("date_to", "")),
        }
        self._history_rows_cache = None
        self._refresh_history()

    def _select_history_task(self, task_id: str) -> None:
        self.selected_history_task_id = task_id
        self._refresh_history_detail(task_id)

    def _open_reconciliation(self, item_id: str) -> None:
        if self._has_active_worker():
            self._show_error("扫描到达安全边界前不能核对 WMS")
            return
        try:
            item = self.repository.get_item(item_id)
            attempt = self.repository.latest_attempt(item_id) or {}
            duplicate_recovery = (
                item.status is ItemStatus.ALREADY_IN_BAG
                and str(attempt.get("recovery_status", ""))
                in {"pending", "in_flight", "failed", "unknown"}
            )
            decision = self.window.open_reconciliation_dialog(
                item,
                attempt,
                duplicate_recovery=duplicate_recovery,
            )
            if decision is None:
                return
            operator = str(decision["operator"])
            reason = str(decision["reason"])
            evidence_ref = str(decision["evidence_ref"])
            reason_with_evidence = f"{reason} | WMS evidence: {evidence_ref}"
            if duplicate_recovery:
                self.repository.reconcile_duplicate_recovery(
                    str(attempt["id"]),
                    tracking=item.tracking,
                    container_no=item.container_no,
                    logid=str(attempt.get("logid") or ""),
                    actor=operator,
                    reason=reason_with_evidence,
                    evidence_ref=evidence_ref,
                )
            else:
                target = ItemStatus(str(decision["target"]))
                check = None
                if target is ItemStatus.READY:
                    if not bool(decision["physical_checked"]):
                        raise ValueError("重新提交前必须重新核验包裹和容器")
                    check = self.physical_check_service.record_manual(
                        item,
                        operator=operator,
                        evidence_ref=f"manual-reconciliation:{evidence_ref}",
                        checked_at_utc=datetime.now(UTC),
                    )
                self.task_service.reconcile_unknown(
                    item.id,
                    target,
                    actor=operator,
                    reason=reason_with_evidence,
                    physical_check=check,
                )
            self._last_refreshed_task = None
            self._refresh_visible_data(force=True)
            self._set_status("WMS 核对已保存")
        except Exception as exc:
            self._show_error(f"无法载入核对记录：{exc}")

    def _show_technical_log(self, task_id: str) -> None:
        activities = self._task_activity_rows(task_id)
        details = "\n".join(
            f"{at}  {level}  {message}" for at, level, message in activities[-100:]
        )
        QMessageBox.information(self.window, "技术日志", details or "无记录")

    def _task_activity_rows(self, task_id: str) -> list[tuple[str, str, str]]:
        activities = [
            (str(row["at_utc"]), str(row["level"]).upper(), str(row["message"]))
            for row in self.repository.logs(task_id=task_id)
        ]
        for row in self.repository.events(task_id=task_id):
            detail = row.get("detail", {})
            summary = ""
            if isinstance(detail, Mapping):
                summary = str(
                    detail.get("reason")
                    or detail.get("outcome")
                    or detail.get("to")
                    or detail.get("tracking")
                    or ""
                )
            message = str(row["event_type"])
            actor = str(row.get("actor") or "")
            if actor:
                message = f"{message} · {actor}"
            if summary:
                message = f"{message} · {summary}"
            activities.append(
                (
                    str(row["at_utc"]),
                    "EVENT",
                    message,
                )
            )
        activities.sort(key=lambda activity: activity[0])
        return [
            (at[11:19], level, message)
            for at, level, message in activities
        ]

    def _export_report(self, kind: str) -> None:
        task_id = self.selected_history_task_id or self.current_task_id
        if not task_id:
            self._show_error("没有可导出的任务")
            return
        suffix = "csv" if kind == "csv" else "xlsx"
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "导出结果",
            str(self.data_dir / f"scan-results.{suffix}"),
            f"{suffix.upper()} (*.{suffix})",
        )
        if not path:
            return
        rows = [self._export_row(item) for item in self.repository.list_items(task_id)]
        try:
            if suffix == "csv":
                self.exporter.export_csv(path, rows)
            else:
                self.exporter.export_xlsx(path, rows)
        except Exception as exc:
            self._show_error(f"导出失败：{exc}")
            return
        self._set_status("导出完成")

    def _export_preview(self, value: object) -> None:
        if not isinstance(value, ImportPreview):
            self._show_error("没有可导出的预览")
            return
        default_name = Path(value.summary.source_name).with_suffix(".preview.xlsx").name
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "导出导入预览",
            str(self.data_dir / default_name),
            "Excel 工作簿 (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        output = Path(path)
        if not output.suffix:
            output = output.with_suffix(".csv" if "csv" in path.casefold() else ".xlsx")
        preview = value
        self.window.set_import_busy(True)
        self._run_job(
            lambda: self._write_preview_export(output, preview),
            lambda saved: self._preview_exported(saved),
            import_job=True,
        )

    def _write_preview_export(self, path: Path, preview: ImportPreview) -> Path:
        fields = (
            "record_type",
            "source_rows",
            "tracking",
            "carrier",
            "status",
            "outbound_at",
            "sort_code",
            "container",
            "reason",
            "raw_json",
        )
        rows: list[dict[str, object]] = []
        for item in preview.candidate_items:
            rows.append(
                {
                    "record_type": "candidate",
                    "source_rows": ",".join(map(str, item.source_row_numbers)),
                    "tracking": item.tracking,
                    "carrier": item.carrier_key,
                    "status": item.status.value,
                    "outbound_at": item.outbound_at_utc.isoformat(),
                    "sort_code": item.sort_code,
                    "container": item.container_no,
                    "reason": "",
                    "raw_json": "",
                }
            )
        source_by_row = {row.row_number: row for row in preview.source_rows}
        mapping = preview.summary.mapping
        for excluded in preview.excluded_rows:
            source = source_by_row.get(excluded.source_row_number)
            values = source.values if source is not None else {}
            rows.append(
                {
                    "record_type": "excluded",
                    "source_rows": str(excluded.source_row_number),
                    "tracking": excluded.tracking,
                    "carrier": str(values.get(mapping.carrier_column, "") or ""),
                    "status": str(values.get(mapping.status_column, "") or ""),
                    "outbound_at": str(values.get(mapping.outbound_at_column, "") or ""),
                    "sort_code": str(
                        values.get(mapping.sort_code_column or "", "") or ""
                    ),
                    "container": "",
                    "reason": excluded.reason,
                    "raw_json": json.dumps(
                        {
                            key: self._json_value(value)
                            for key, value in values.items()
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        if path.suffix.casefold() == ".csv":
            return self.exporter.export_csv(path, rows, fieldnames=fields)
        return self.exporter.export_xlsx(path, rows, fieldnames=fields)

    def _preview_exported(self, saved: object) -> None:
        self.window.set_import_busy(False)
        self._set_status(f"预览已导出：{Path(str(saved)).name}")

    def _export_row(self, item: QueueItem) -> dict[str, object]:
        attempt = self.repository.latest_attempt(item.id) or {}
        check = self.repository.get_physical_check(item.id)
        evidence = self._attempt_evidence(attempt)
        attempt_id = str(attempt.get("id") or "")
        scan_operator = next(
            (
                str(event.get("actor") or "")
                for event in reversed(self.repository.events(task_id=item.task_id, item_id=item.id))
                if event.get("attempt_id") == attempt_id
                and event.get("event_type") == "attempt_started"
            ),
            "",
        )
        return {
            "tracking": item.tracking,
            "carrier": item.carrier_key,
            "sort": item.sort_code,
            "container": item.container_no,
            "source_rows": ",".join(map(str, item.source_row_numbers)),
            "outbound_at_utc": item.outbound_at_utc.isoformat(),
            "status": item.status.value,
            "attempt_id": attempt_id,
            "attempt_started_at_utc": attempt.get("started_at_utc", ""),
            "result_at_utc": attempt.get("finished_at_utc", ""),
            "logid": evidence.get("logid", ""),
            "operator": scan_operator,
            "physical_check_operator": check.operator if check else "",
            "physical_evidence_ref": (
                check.evidence_ref if check else ""
            ),
            "physical_check_revoked_at_utc": (
                check.revoked_at_utc.isoformat()
                if check and check.revoked_at_utc is not None
                else ""
            ),
        }

    def _download_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self.window, "保存 XLSX 模板", "outbound-report-template.xlsx", "Excel 工作簿 (*.xlsx)"
        )
        if not path:
            return
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Outbound"
        sheet.append(["tracking", "carrier", "status", "outbound_at", "sort_code"])
        try:
            workbook.save(path)
        except OSError as exc:
            self._show_error(f"模板保存失败：{exc}")
            return
        self._set_status("XLSX 模板已保存")

    def _backup_database(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "备份本地记录",
            str(self.data_dir / f"{self.mode.value}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.sqlite3"),
            "SQLite 数据库 (*.sqlite3)",
        )
        if not path:
            return
        try:
            self.repository.backup_to(path)
        except Exception as exc:
            self._show_error(f"备份失败：{exc}")
            return
        self._set_status("本地记录已备份")

    def _restore_database(self) -> None:
        if self._has_active_worker():
            self._show_error("扫描运行中不能恢复数据库")
            return
        if self._has_running_jobs():
            self._show_error("后台操作结束后再恢复数据库")
            return
        source, _ = QFileDialog.getOpenFileName(
            self.window, "选择数据库备份", str(self.data_dir), "SQLite 数据库 (*.sqlite3)"
        )
        if not source:
            return
        answer = QMessageBox.question(
            self.window,
            "恢复记录",
            "恢复会替换当前模式的本地记录。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        target = self.repository.path
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        preserved = target.with_name(f"{target.stem}.before-restore-{timestamp}{target.suffix}")
        staging = target.with_name(f".{target.stem}.restore-{uuid4().hex}{target.suffix}")
        preserved_current = False
        replacement_installed = False
        original_repository = self.repository
        replacement_repository: TaskRepository | None = None
        try:
            original_repository.close()
            TaskRepository.restore_from(source, staging, expected_mode=self.mode)
            if target.exists():
                target.replace(preserved)
                preserved_current = True
            staging.replace(target)
            replacement_installed = True
            replacement_repository = TaskRepository(target)
            self.repositories[self.mode] = replacement_repository
            self.task_services[self.mode] = TaskService(self.repository, actor=self._actor)
            recover_on_startup(self.repository)
            self.wms_controller = None
            self.verified_session = None
            self._controller_invalidated = True
            self.real_mode_authorized = False
            self._set_authorization_check(False)
            self.window.set_device_status(connected=self._connected, session_verified=False)
            self.repository.append_log(
                "info",
                "database_restored",
                detail={"source": str(Path(source).resolve()), "mode": self.mode.value},
            )
            self._select_latest_task()
            self._history_rows_cache = None
            self._last_refreshed_task = None
            self._refresh_visible_data(force=True)
            self._set_status("本地记录已恢复")
        except Exception as exc:
            if staging.exists():
                staging.unlink()
            current_repository = self.repositories[self.mode]
            if current_repository is replacement_repository and replacement_repository is not None:
                replacement_repository.close()
            if replacement_installed and target.exists():
                target.unlink()
            if preserved_current and preserved.exists():
                preserved.replace(target)
            if target.exists():
                self.repositories[self.mode] = TaskRepository(target)
                self.task_services[self.mode] = TaskService(self.repository, actor=self._actor)
            else:
                self.repositories[self.mode] = original_repository
                self.task_services[self.mode] = TaskService(self.repository, actor=self._actor)
            self._show_error(f"恢复失败：{exc}")

    def _current_wms_version(self) -> str:
        if self.mode is DeviceMode.SIMULATED:
            return "模拟 WMS"
        return self._wms_app_version or "WMS 版本未读取"

    def _run_diagnostics(self) -> None:
        adb_path = str(self.settings.get("adb_path") or shutil.which("adb") or "adb")
        adb_ok = (
            Path(adb_path).is_file()
            if Path(adb_path).parent != Path(".")
            else shutil.which(adb_path) is not None
        )
        results = {
            "adb": "可用" if adb_ok else "缺失",
            "driver": (
                "模拟"
                if self.mode is DeviceMode.SIMULATED
                else (
                    "环境已配置"
                    if os.environ.get("ADB_LIBUSB", "").strip() == "1"
                    else "待检查"
                )
            ),
            "database": "已初始化" if self.repository.path.is_file() else "不可用",
            "device": "已授权" if self._connected else "未连接",
            "wms": "会话有效" if self._verified_session_valid() else "未验证",
            "wms_version": self._current_wms_version()
            if self.mode is DeviceMode.SIMULATED or self._wms_app_version
            else "未读取",
        }
        self.window.set_diagnostics(results)

    def close(self) -> None:
        if hasattr(self, "_poll_timer"):
            self._poll_timer.stop()
        workers = []
        for mode, service in self.task_services.items():
            for task in self.repositories[mode].list_tasks():
                worker = service.active_worker(task.id)
                if worker is None:
                    continue
                try:
                    service.stop_task(task.id)
                except RepositoryError:
                    worker.request_stop()
                workers.append(worker)
        for worker in workers:
            worker.join(timeout=60)
        for job in tuple(self._jobs):
            job.wait(30000)
        for repository in self.repositories.values():
            repository.close()
