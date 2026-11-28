"""Wire the desktop UI to the real PDA services."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from tt_auto_bag_scanner.application.import_service import (
    ImportPreview,
    ImportService,
    suggest_column_mapping,
)
from tt_auto_bag_scanner.application.startup_recovery import recover_on_startup
from tt_auto_bag_scanner.application.task_service import TaskService
from tt_auto_bag_scanner.domain.device import DeviceAdapter
from tt_auto_bag_scanner.domain.models import (
    ALREADY_GROUPED_REASON,
    CarrierConfig,
    DeviceInfo,
    DeviceMode,
    ImportMapping,
    ItemStatus,
    QueueItem,
    ScanOutcome,
    TaskCounts,
    TaskSnapshot,
    TaskState,
    VerifiedSession,
)
from tt_auto_bag_scanner.infrastructure.adb_adapter import AdbDeviceAdapter
from tt_auto_bag_scanner.infrastructure.carrier_config import (
    CarrierConfigService,
    pending_carrier_pairs,
)
from tt_auto_bag_scanner.infrastructure.device_manager import DeviceManager
from tt_auto_bag_scanner.infrastructure.excel_parser import ExcelParser, WorkbookValidationError
from tt_auto_bag_scanner.infrastructure.exporter import ReportExporter
from tt_auto_bag_scanner.infrastructure.paths import ensure_app_data_dir
from tt_auto_bag_scanner.infrastructure.physical_check_service import PhysicalCheckService
from tt_auto_bag_scanner.infrastructure.repository import RepositoryError, TaskRepository
from tt_auto_bag_scanner.infrastructure.wms_controller import (
    MixedSortController,
    WmsController,
)
from tt_auto_bag_scanner.ui.main_window import (
    DEFAULT_REPORT_TIMEZONE,
    SCAN_SPEED_LABELS,
    MainWindow,
)

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
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_failure = on_failure
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
        adapter_factory: Callable[[str], DeviceAdapter] | None = None,
    ) -> None:
        super().__init__(window)
        self._preview = QProcess(self)
        self._preview.setStandardOutputFile(QProcess.nullDevice())
        self._preview.setStandardErrorFile(QProcess.nullDevice())
        self._preview.errorOccurred.connect(self._preview_error)
        self._preview.finished.connect(self._preview_finished)
        self.window = window
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else ensure_app_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.data_dir / "settings.json"
        self.settings = self._load_settings()
        self.real_mode_authorized = False
        self.mode = DeviceMode.REAL
        self._adapter_factory = adapter_factory or self._new_adb_adapter
        self.repositories = {
            DeviceMode.REAL: TaskRepository(self.data_dir / "real.sqlite3"),
        }
        self.task_services = {
            DeviceMode.REAL: TaskService(self.repositories[DeviceMode.REAL], actor=self._actor),
        }
        for repository in self.repositories.values():
            recover_on_startup(repository)

        self.import_service = ImportService()
        self.parser = ExcelParser()
        self.carrier_service = CarrierConfigService()
        self.physical_check_service = PhysicalCheckService()
        self.exporter = ReportExporter()
        self.adapters: dict[DeviceMode, DeviceAdapter] = {}
        self.device_managers: dict[DeviceMode, DeviceManager] = {}
        self._make_device_components()

        self.current_task_id: str | None = None
        self.current_preview: ImportPreview | None = None
        self._last_preview_request: dict[str, object] | None = None
        self.selected_history_task_id: str | None = None
        self.wms_controller: WmsController | None = None
        self.verified_session: VerifiedSession | None = None
        self._failed_container_checks: set[tuple[str, str, str]] = set()
        self._controller_invalidated = False
        self._connected = False
        self._wms_app_version = ""
        self._jobs: set[OperationJob] = set()
        # (mode, task id, task updated_at, item status counts): items finish mid-run
        # without a task transition, so the counts keep the progress live.
        self._last_refreshed_task: tuple[object, ...] | None = None
        self._last_activity_revision: tuple[str, int | None, int | None] | None = None
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
        self._select_latest_task()
        self._refresh_visible_data(force=True)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(max(150, poll_interval_ms))
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()
        if self.settings.get("auto_connect_device") is True:
            QTimer.singleShot(0, self._auto_connect_device)

    @property
    def repository(self) -> TaskRepository:
        return self.repositories[self.mode]

    @property
    def task_service(self) -> TaskService:
        return self.task_services[self.mode]

    @property
    def adapter(self) -> DeviceAdapter:
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

    def _new_adb_adapter(self, adb_path: str) -> DeviceAdapter:
        return AdbDeviceAdapter(package_name=WMS_PACKAGE, adb_path=adb_path)

    def _find_local_adb(self) -> str | None:
        on_path = shutil.which("adb")
        if on_path:
            return on_path
        sdk_roots = [Path(value) for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
                     if (value := os.environ.get(name))]
        if local_app_data := os.environ.get("LOCALAPPDATA"):
            sdk_roots.append(Path(local_app_data) / "Android" / "Sdk")
        candidates = [root / "platform-tools" / "adb.exe" for root in sdk_roots]
        candidates.append(self.data_dir / "platform-tools" / "adb.exe")
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).parent / "platform-tools" / "adb.exe")
        return next((str(path.resolve()) for path in candidates if path.is_file()), None)

    def _make_device_components(self) -> None:
        configured_path = str(self.settings.get("adb_path") or "").strip()
        detected_path = None if configured_path else self._find_local_adb()
        self._adb_path = configured_path or detected_path or "adb"
        self.window.adb_path_edit.setPlaceholderText(detected_path or "未找到 ADB")
        adapter = self._adapter_factory(self._adb_path)
        self.adapters[DeviceMode.REAL] = adapter
        self.device_managers[DeviceMode.REAL] = DeviceManager(adapter)

    def _new_wms_controller(
        self,
        adapter: DeviceAdapter,
        *,
        warehouse: str,
        real_submission_authorized: bool = False,
    ) -> WmsController:
        timeout = float(self.settings.get("response_timeout_seconds", 15))
        interval = 0.2
        controller_class = (
            MixedSortController if self.settings.get("sort_mode") == "mixed" else WmsController
        )
        return controller_class(
            adapter,
            warehouse=warehouse,
            real_submission_authorized=real_submission_authorized,
            poll_attempts=max(1, math.ceil(timeout / interval)),
            poll_interval_seconds=interval,
            timeout_seconds=timeout,
        )

    def _connect_window(self) -> None:
        window = self.window
        window.timezone_combo.currentIndexChanged.connect(
            lambda: self._refresh_visible_data(force=True)
        )
        window.open_report_requested.connect(self._choose_report)
        window.preview_requested.connect(self._build_preview)
        window.clear_task_requested.connect(self._clear_task)
        window.preview_export_requested.connect(self._export_preview)
        window.sort_code_overrides_requested.connect(self._rebuild_preview_with_sort_overrides)
        window.review_task_requested.connect(self._review_task)
        window.download_template_requested.connect(self._download_template)
        window.excluded_rows_requested.connect(lambda: None)
        window.device_refresh_requested.connect(self._refresh_devices)
        window.device_connect_requested.connect(self._connect_device)
        window.physical_check_requested.connect(self._record_physical_check)
        window.cancel_item_requested.connect(self._cancel_unsubmitted_item)
        window.carrier_save_requested.connect(self._save_carrier_config)
        window.container_verify_requested.connect(self._verify_container)
        window.import_source_changed.connect(self._reload_report_headers)
        window.settings_save_requested.connect(self._save_window_settings)
        window.scan_speed_requested.connect(self._set_scan_speed)
        window.sort_mode_requested.connect(self._set_sort_mode)
        window.visible_queue_items_changed.connect(self._set_priority_items)
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
        window.workspace_confirmation_requested.connect(self._confirm_workspace_result)
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
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        job = OperationJob(
            operation,
            on_success,
            self,
            import_job=import_job,
            device_job=device_job,
            cleanup=cleanup,
            on_failure=on_failure,
        )
        self._jobs.add(job)
        job.succeeded.connect(self._job_succeeded)
        job.failed.connect(self._job_failed)
        job.finished.connect(self._job_finished)
        if device_job:
            self.window.set_device_busy(True)
            self._set_status("PDA 操作进行中…")
        job.start()

    def _job_succeeded(self, result: object) -> None:
        job = self.sender()
        if isinstance(job, OperationJob):
            job.on_success(result)

    def _job_failed(self, message: str) -> None:
        job = self.sender()
        if isinstance(job, OperationJob) and job.is_import_job:
            self.window.set_import_busy(False)
        if isinstance(job, OperationJob) and job.on_failure is not None:
            job.on_failure()
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
            if job.is_device_job and not self._has_pending_device_jobs():
                self.window.set_device_busy(False)

    def _has_active_worker(self) -> bool:
        return any(
            service.active_worker(task.id) is not None
            for mode, service in self.task_services.items()
            for task in self.repositories[mode].list_tasks()
        )

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
        minimum = int(values.get("scan_interval_min_seconds", 5))
        maximum = int(values.get("scan_interval_max_seconds", 30))
        if minimum > maximum:
            self._show_error("最短扫描间隔不能超过最长扫描间隔")
            return
        warehouse_changed = values.get("warehouse") != self.settings.get("warehouse")
        adb_changed = values.get("adb_path") != self.settings.get("adb_path")
        timeout_changed = (
            values.get("response_timeout_seconds")
            != self.settings.get("response_timeout_seconds")
        )
        self.settings.update(values)
        self._write_settings()
        if adb_changed:
            self._make_device_components()
            self._connected = False
            self.real_mode_authorized = False
        if warehouse_changed or timeout_changed or adb_changed:
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
        self._set_status("设置已保存")
        self._refresh_gates()

    def _set_sort_mode(self, mode: str) -> None:
        previous = str(self.settings.get("sort_mode", "single"))
        if mode not in ("single", "mixed") or mode == previous:
            self.window.set_sort_mode(previous)
            return
        if self._reject_pending_device_job("切换排序模式") or self._has_active_worker():
            if self._has_active_worker():
                self._show_error("扫描运行中不能切换排序模式")
            self.window.set_sort_mode(previous)
            return
        self.settings["sort_mode"] = mode
        try:
            self._write_settings()
        except OSError as exc:
            self.settings["sort_mode"] = previous
            self.window.set_sort_mode(previous)
            self._show_error(f"排序模式保存失败：{exc}")
            return
        self.wms_controller = None
        self.verified_session = None
        self._controller_invalidated = True
        self.window.set_device_status(connected=self._connected, session_verified=False)
        self.window.set_sort_mode(mode)

    def _set_priority_items(self, item_ids: list[str]) -> None:
        if self.current_task_id is not None:
            worker = self.task_service.active_worker(self.current_task_id)
            if worker is not None:
                worker.set_priority_items(item_ids)

    def _set_scan_speed(self, speed: str) -> None:
        if speed not in SCAN_SPEED_LABELS:
            return
        previous = self.settings.get("scan_speed", "normal")
        self.settings["scan_speed"] = speed
        try:
            self._write_settings()
        except OSError as exc:
            self.settings["scan_speed"] = previous
            self.window.set_scan_speed(str(previous))
            self._show_error(f"扫描速度保存失败：{exc}")
            return
        self.window.set_scan_speed(speed)
        if self.current_task_id is not None:
            worker = self.task_service.active_worker(self.current_task_id)
            if worker is not None:
                worker.set_scan_speed(speed)

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
        self._run_job(self.device_manager.refresh, self._devices_refreshed, device_job=True)

    def _devices_refreshed(self, result: object) -> None:
        devices = tuple(result)  # type: ignore[arg-type]
        self.window.set_devices(devices)
        self._connected = self.device_manager.connection_state.value == "connected"
        if not self._connected:
            self.real_mode_authorized = False
            self._wms_app_version = ""
            self.verified_session = None
            if self.wms_controller is not None:
                self.wms_controller.real_submission_authorized = False
                self.wms_controller.invalidate_session()
            self.window.set_device_status(connected=False, session_verified=False)
        self._set_status(f"发现 {len(devices)} 台设备")
        self._refresh_gates()

    def _auto_connect_device(self) -> None:
        if self._connected or self._has_active_worker():
            return
        self._run_job(self.device_manager.refresh, self._auto_connect_refreshed, device_job=True)

    def _auto_connect_refreshed(self, result: object) -> None:
        self._devices_refreshed(result)
        serials = [device.serial for device in tuple(result)]  # type: ignore[arg-type]
        saved = str(self.settings.get("last_device_serial") or "")
        serial = saved if saved in serials else (serials[0] if len(serials) == 1 else "")
        if not serial:
            self._set_status("自动连接：未找到唯一设备")
            return
        index = self.window.device_combo.findData(serial)
        if index >= 0:
            self.window.device_combo.setCurrentIndex(index)
        QTimer.singleShot(0, lambda: self._connect_device(serial))

    def _connect_device(self, serial: str) -> None:
        if self._reject_pending_device_job("连接设备"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能重新连接设备")
            return
        if not serial.strip():
            self._show_error("请选择设备")
            return
        if not str(self.settings.get("operator_id", "")).strip():
            self._show_error("请先填写并保存操作员 ID")
            return

        self._connected = False
        self.real_mode_authorized = False
        self._wms_app_version = ""
        self.wms_controller = None
        self.verified_session = None
        self.window.set_device_status(connected=False, session_verified=False)
        self._refresh_gates()

        def connect() -> DeviceInfo:
            device = self.device_manager.connect(serial)
            self.device_manager.launch_app(WMS_PACKAGE)
            return device

        self._run_job(connect, self._device_connected, device_job=True)

    def _device_connected(self, result: DeviceInfo) -> None:
        self._connected = True
        if self.settings.get("last_device_serial") != result.serial:
            self.settings["last_device_serial"] = result.serial
            self._write_settings()
        self.real_mode_authorized = True
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
        self._open_device_preview(result.serial)

    def _open_device_preview(self, serial: str) -> None:
        self._stop_device_preview()
        search_roots = [self.data_dir]
        if bundle_dir := getattr(sys, "_MEIPASS", None):
            search_roots.insert(0, Path(bundle_dir))
        candidates = (root / "tools" / "scrcpy" / "scrcpy.exe" for root in search_roots)
        executable = next((str(path) for path in candidates if path.is_file()), None)
        executable = executable or shutil.which("scrcpy")
        if not executable:
            self._set_status("未找到 scrcpy · 请在 PDA 手动登录")
            return

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("ADB", shutil.which(self._adb_path) or self._adb_path)
        self._preview.setProcessEnvironment(environment)
        self._preview.start(executable, ["--serial", serial, "--no-control", "--no-audio"])

    def _preview_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._set_status("投屏启动失败 · 请在 PDA 手动登录")

    def _preview_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if exit_status == QProcess.ExitStatus.NormalExit and exit_code != 0:
            self._set_status("投屏失败 · 请在 PDA 手动操作")

    def _stop_device_preview(self) -> None:
        if self._preview.state() != QProcess.ProcessState.NotRunning:
            self._preview.kill()
            self._preview.waitForFinished(1000)

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
        saved_columns = self._import_defaults().get("columns")
        self.window.set_import_headers(
            headers,
            selected_columns=suggest_column_mapping(
                headers,
                saved=saved_columns if isinstance(saved_columns, Mapping) else None,
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
        saved_columns = self._import_defaults().get("columns")
        self.window.set_import_headers(
            headers,
            selected_columns=suggest_column_mapping(
                headers,
                saved=saved_columns if isinstance(saved_columns, Mapping) else None,
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
            if not mapping.report_timezone:
                raise ValueError("请选择报表时区")
            if not path or not mapping.sheet_name:
                raise ValueError("请选择 XLSX 报表")
        except (KeyError, TypeError, ValueError) as exc:
            self._show_error(str(exc))
            return
        task_id = str(uuid4())
        mode = self.mode
        if not self.real_mode_authorized:
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
        if repository is self.repository and preview.unconfigured_carriers:
            pending_count = self._remember_pending_carriers(preview.unconfigured_carriers)
            self._set_status(f"{pending_count} 个承运商待填大包号")
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
        pending = len(self._pending_carriers())
        self._set_status(f"预览已保存 · {pending} 个承运商待填大包号" if pending else "预览已保存")

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
        if missing and self._skip_physical_enabled():
            if not self._apply_authorized_skips():
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
        self.window.navigate_to("扫描工作区")
        self._refresh_visible_data()

    def _skip_physical_enabled(self) -> bool:
        return self.settings.get("skip_physical_check") is True

    def _apply_authorized_skips(self) -> bool:
        """Saved setting authorizes the skip; each item keeps an authorized_skip record."""
        task_id = self.current_task_id
        if not task_id:
            return False
        operator = str(self.settings.get("operator_id", "")).strip()
        if not operator:
            self._show_error("跳过实物核验需要先在设置中填写操作员")
            return False
        items = self.repository.list_items(task_id)
        waiting = [item for item in items if item.status is ItemStatus.WAIT_PHYSICAL]
        now = datetime.now(UTC)
        try:
            for item in waiting:
                check = self.physical_check_service.record_authorized_skip(
                    item, operator=operator, checked_at_utc=now
                )
                self.repository.record_physical_check(check, actor=operator)
            if self.repository.get_task(task_id).state is TaskState.DRAFT:
                self.repository.transition_task(
                    task_id,
                    TaskState.READY,
                    actor=operator,
                    reason="physical check skipped by saved site setting",
                )
        except Exception as exc:
            self._show_error(f"跳过核验未完成：{exc}")
            self._refresh_visible_data(force=True)
            return False
        if waiting:
            self._set_status(f"{len(waiting)} 项已授权跳过实物核验")
        return True

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
        self._set_status("承运商配置已保存")
        self._refresh_gates()

    def _pending_carriers(self) -> tuple[tuple[str, str], ...]:
        saved = self.settings.get("pending_carriers")
        pairs = (
            tuple(
                (str(pair[0]), str(pair[1]))
                for pair in saved
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            )
            if isinstance(saved, list)
            else ()
        )
        return pending_carrier_pairs(pairs, (), self.repository.list_carrier_configs())

    def _remember_pending_carriers(self, report_pairs: Iterable[tuple[str, str]]) -> int:
        pending = pending_carrier_pairs(
            self._pending_carriers(), report_pairs, self.repository.list_carrier_configs()
        )
        self.settings["pending_carriers"] = [list(pair) for pair in pending]
        self._write_settings()
        self.window.set_carrier_rows(self._carrier_rows())
        return len(pending)

    def _carrier_rows(self) -> tuple[dict[str, object], ...]:
        configured = tuple(
            {
                "carrier_key": item.carrier_key,
                "display_name": item.display_name,
                "aliases": item.aliases,
                "sort_code": item.sort_code,
                "container_no": item.container_no,
                "enabled": item.enabled,
                "verification": self._container_verification(item),
            }
            for item in self.repository.list_carrier_configs()
        )
        pending = tuple(
            {
                "carrier_key": carrier,
                "display_name": carrier,
                "aliases": (),
                "sort_code": sort_code,
                "container_no": "",
                "enabled": False,
                "pending": True,
            }
            for carrier, sort_code in self._pending_carriers()
        )
        return configured + pending

    @staticmethod
    def _container_check_key(config: CarrierConfig) -> tuple[str, str, str]:
        return (config.carrier_key.casefold(), config.sort_code, config.container_no)

    def _container_verification(self, config: CarrierConfig) -> str:
        session = self.verified_session
        if (
            session is not None
            and session.valid
            and session.sort_code == config.sort_code
            and session.container_no == config.container_no
        ):
            return "verified"
        if self._container_check_key(config) in self._failed_container_checks:
            return "failed"
        return "unverified"

    def _container_check_failed(self, config: CarrierConfig) -> None:
        self._failed_container_checks.add(self._container_check_key(config))
        session = self.verified_session
        if (
            session is not None
            and session.sort_code == config.sort_code
            and session.container_no == config.container_no
        ):
            self.verified_session = None
        self.window.set_carrier_rows(self._carrier_rows())

    def _verify_container(self, carrier_key: str) -> None:
        if self._reject_pending_device_job("核验或切换容器"):
            return
        if self._has_active_worker():
            self._show_error("扫描运行中不能切换容器")
            return
        if self.current_task_id and self.repository.has_blocking_unresolved(
            self.current_task_id
        ):
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
        if not self.real_mode_authorized:
            self._show_error("实机操作未授权")
            return
        if not self._connected:
            self._show_error("请先连接 PDA")
            return
        warehouse = str(self.settings.get("warehouse", "")).strip()
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
            # A fresh controller holds no session; never switch from a stale one.
            self.verified_session = None
        controller = self.wms_controller
        mode = self.mode
        adapter = self.adapter
        current_session = self.verified_session
        unresolved_submissions = int(
            self.current_task_id is not None
            and self.repository.has_blocking_unresolved(self.current_task_id)
        )

        def verify() -> VerifiedSession:
            # Only the controller's own session can be switched away from.
            if getattr(controller, "active_session", None) is None:
                return controller.verify_current_or_open_bag(
                    config, unresolved_submissions=unresolved_submissions
                )
            if current_session is not None and (
                current_session.container_no != config.container_no
                or current_session.sort_code != config.sort_code
            ):
                return controller.switch_bag(
                    config,
                    unresolved_submissions=unresolved_submissions,
                )
            return controller.verify_current_or_open_bag(
                config, unresolved_submissions=unresolved_submissions
            )

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
            on_failure=lambda: self._container_check_failed(config),
        )

    def _container_verified(
        self,
        controller: WmsController,
        session: object,
        config: CarrierConfig,
        *,
        expected_mode: DeviceMode,
        expected_adapter: DeviceAdapter,
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
        self._failed_container_checks.discard(self._container_check_key(config))
        self.window.set_carrier_rows(self._carrier_rows())
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
        if not self.real_mode_authorized:
            self._show_error("实机操作未授权")
            return
        if not self._connected:
            self._show_error("请先连接 PDA")
            return
        warehouse = str(self.settings.get("warehouse") or "").strip()
        if not warehouse:
            self._show_error("请在设置中填写 WMS 仓库标识")
            return
        if self._skip_physical_enabled() and not self._apply_authorized_skips():
            return
        if self._controller_invalidated or self.wms_controller is None:
            self.wms_controller = self._new_wms_controller(
                self.adapter, warehouse=warehouse, real_submission_authorized=True
            )
            self._controller_invalidated = False
            self.verified_session = None
        controller = self.wms_controller

        def session_provider(item: QueueItem) -> VerifiedSession:
            return self._provide_session(controller, task_id, item)

        try:
            self.task_service.start_task(
                task_id,
                controller,
                session_provider,
                real_mode_authorized=self.real_mode_authorized,
                scan_interval=(
                    int(self.settings.get("scan_interval_min_seconds", 5)),
                    int(self.settings.get("scan_interval_max_seconds", 30)),
                    str(self.settings.get("scan_speed", "normal")),
                ),
                priority_item_ids=self.window.visible_workspace_item_ids(),
            )
        except Exception as exc:
            self._show_error(f"任务未启动：{exc}")
            self._refresh_visible_data()
            return
        self.window.set_running(True)
        self._set_status("任务运行中")
        self._poll()

    def _provide_session(
        self, controller: WmsController, task_id: str, item: QueueItem
    ) -> VerifiedSession:
        current = self.verified_session
        target = self._config_for_item(item)
        blocking = int(self.repository.has_blocking_unresolved(task_id))
        if getattr(controller, "active_session", None) is None:
            # An UNKNOWN result dropped the controller session: re-read the page and
            # verify, back out of another bag, or open the target bag.
            session = controller.verify_current_or_open_bag(
                target, unresolved_submissions=blocking
            )
        elif current is not None and (
            current.container_no == item.container_no
            and current.sort_code == item.sort_code
            and current.warehouse == str(self.settings.get("warehouse") or "")
        ):
            return current
        elif current is not None:
            session = controller.switch_bag(target, unresolved_submissions=blocking)
        else:
            session = controller.open_bag(target)
        self.verified_session = session
        return session

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

    def _clear_task(self) -> None:
        """Stop and unload the current task; its records stay in task history."""
        if self._reject_pending_device_job("清空任务"):
            return
        task_id = self.current_task_id
        if not task_id:
            self._show_error("没有可清空任务")
            return
        if self.repository.has_blocking_unresolved(task_id):
            # UNKNOWN items stay in task history for reconciliation; only
            # IN_FLIGHT and unfinished duplicate recovery block clearing.
            self._show_error("有提交中的包裹，请先完成核对")
            return
        worker = self.task_service.active_worker(task_id)
        task = self.repository.get_task(task_id)
        if (worker is not None and worker.is_alive()) or task.state in {
            TaskState.RUNNING,
            TaskState.PAUSING,
        }:
            self._show_error("请先停止任务")
            return
        try:
            if task.state not in {TaskState.STOPPED, TaskState.COMPLETED}:
                self.task_service.stop_task(task_id)
        except Exception as exc:
            self._show_error(f"清空失败：{exc}")
            return
        self.current_task_id = None
        self.selected_history_task_id = None
        self.current_preview = None
        self.window.clear_import_preview()
        self._last_refreshed_task = None
        self._history_rows_cache = None
        self._refresh_visible_data(force=True)
        self._set_status("任务已清空")

    def _select_latest_task(self) -> None:
        tasks = [
            task
            for task in self.repository.list_task_summaries()
            if task["state"] is not TaskState.STOPPED
        ]
        self.current_task_id = str(tasks[-1]["id"]) if tasks else None
        self.selected_history_task_id = self.current_task_id
        self._last_refreshed_task = None

    def _refresh_visible_data(self, *, force: bool = False) -> None:
        current_updated_at: datetime | None = None
        current_counts: tuple[int, ...] = ()
        if self.current_task_id:
            try:
                current = self.repository.get_task(self.current_task_id)
                current_updated_at = current.updated_at_utc
                current_counts = tuple(current.counts.by_status.values())
            except RepositoryError:
                self.current_task_id = None
        marker = (self.mode, self.current_task_id, current_updated_at, current_counts)
        if not force and marker == self._last_refreshed_task:
            return
        self._last_refreshed_task = marker
        self.window.set_carrier_rows(self._carrier_rows())
        if self.current_task_id:
            try:
                items = self.repository.list_items(self.current_task_id)
                self.window.set_queue_items(items)
                task = self.repository.get_task(self.current_task_id)
                self.window.set_current_task(task.name, task.state)
                stats = self.repository.statistics(self.current_task_id)
                self.window.set_counts(
                    counts=stats,
                    attempted=stats.get("attempts", 0),
                    total=stats.get("total", len(items)),
                )
                self._refresh_activity(self.current_task_id, items=items)
            except RepositoryError as exc:
                self._show_error(str(exc))
        else:
            self.window.set_queue_items(())
            self.window.set_current_task("")
            self.window.set_counts(success=0, duplicates=0, unknown=0, total=0)
            self.window.set_activity_rows(())
            self._last_activity_revision = None
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
        self.window.set_task_preflight(
            device_ready=self._device_ready() and self.real_mode_authorized,
            task_ready=self._task_ready(),
            physical_checks_complete=(
                all(item.status is not ItemStatus.WAIT_PHYSICAL for item in items)
                or self._skip_physical_enabled()
            )
            and bool(items),
            unresolved_count=self._unresolved_count(),
            real_mode_authorized=self.real_mode_authorized,
        )

    def _device_ready(self) -> bool:
        return self._connected and bool(str(self.settings.get("warehouse") or "").strip())

    def _task_ready(self) -> bool:
        if not self.current_task_id:
            return False
        try:
            task = self.repository.get_task(self.current_task_id)
        except RepositoryError:
            return False
        if task.state is TaskState.DRAFT and self._skip_physical_enabled():
            return True
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
        # Only blocking work stops a start; UNKNOWN items stay listed for later review.
        return int(self.repository.has_blocking_unresolved(self.current_task_id))

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
        marker = (
            self.mode,
            task.id,
            task.updated_at_utc,
            tuple(task.counts.by_status.values()),
        )
        if marker == self._last_refreshed_task:
            self._refresh_activity(task.id)
            return
        self.window.set_running(task.state in {TaskState.RUNNING, TaskState.PAUSING})
        if task.state is TaskState.BLOCKED:
            self._controller_invalidated = True
            self.verified_session = None
            if self.wms_controller is not None:
                self.wms_controller.invalidate_session()
                self.wms_controller.real_submission_authorized = False
            self.window.set_device_status(connected=self._connected, session_verified=False)
            if not self.repository.has_blocking_unresolved(task.id):
                try:
                    self.repository.transition_task(
                        task.id,
                        TaskState.PAUSED,
                        actor=self._actor,
                        reason="worker stopped with no unresolved WMS submissions",
                    )
                    self._set_status("任务已暂停；请在活动记录查看原因")
                except RepositoryError:
                    self._set_status("任务已阻止；请查看任务记录")
            else:
                self._set_status("任务已阻止；请先核对 WMS")
        elif task.state is TaskState.STOPPED:
            self._controller_invalidated = True
            self.verified_session = None
            self.real_mode_authorized = False
            if self.wms_controller is not None:
                self.wms_controller.real_submission_authorized = False
                self.wms_controller.invalidate_session()
            self.window.set_device_status(connected=self._connected, session_verified=False)
            self._set_status("任务已停止；恢复前请重新连接设备")
        elif task.state in {TaskState.RUNNING, TaskState.PAUSED, TaskState.COMPLETED}:
            session = getattr(self.wms_controller, "active_session", None)
            self.window.set_device_status(
                connected=self._connected,
                warehouse=session.warehouse if session else "",
                container_no=session.container_no if session else "",
                app_version=self._current_wms_version(),
                session_verified=bool(session and session.valid),
            )
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

    def _confirm_workspace_result(self, item_id: str) -> None:
        if self._has_active_worker():
            self._show_error("扫描到达安全边界前不能确认通过")
            return
        try:
            item = self.repository.get_item(item_id)
            if item.task_id != self.current_task_id or item.status is not ItemStatus.UNKNOWN:
                return
            self.task_service.reconcile_unknown(
                item.id,
                ItemStatus.SUCCESS,
                actor=self._actor,
                reason="人工确认通过",
            )
        except RepositoryError as exc:
            self._show_error(f"确认通过未保存：{exc}")
            return
        self._last_refreshed_task = None
        self._refresh_visible_data(force=True)
        self._set_status("已确认通过")

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

    def _refresh_activity(self, task_id: str, *, items: Sequence[QueueItem] | None = None) -> None:
        revision = (task_id, *self.repository.activity_revision(task_id))
        if items is None:
            if revision == self._last_activity_revision:
                return
            items = self.repository.list_items(task_id)
        self.window.set_activity_rows(
            self._task_activity_rows(task_id, items_by_id={item.id: item for item in items})[-100:]
        )
        self._last_activity_revision = revision

    def _task_activity_rows(
        self, task_id: str, *, items_by_id: Mapping[str, QueueItem] | None = None
    ) -> list[tuple[str, str, str]]:
        timezone = ZoneInfo(self.window.timezone_combo.currentData() or DEFAULT_REPORT_TIMEZONE)
        scan_labels = {
            ScanOutcome.SUCCESS: "扫描成功",
            ScanOutcome.ALREADY_IN_BAG: "重复扫描，已跳过",
        }
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
            if items_by_id is not None and message == "attempt_finished":
                item = items_by_id.get(str(row.get("item_id")))
                label = scan_labels.get(summary)
                if summary == ScanOutcome.REJECTED and isinstance(detail, Mapping):
                    evidence = detail.get("evidence")
                    if (
                        isinstance(evidence, Mapping)
                        and evidence.get("error_text") == ALREADY_GROUPED_REASON
                    ):
                        label = ALREADY_GROUPED_REASON
                if item is not None and label:
                    message = f"{label}：{item.carrier_key} · {item.tracking}"
                    activities.append((str(row["at_utc"]), "EVENT", message))
                    continue
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
            (datetime.fromisoformat(at).astimezone(timezone).strftime("%H:%M:%S"), level, message)
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
        return self._wms_app_version or "WMS 版本未读取"

    def _run_diagnostics(self) -> None:
        adb_path = self._adb_path
        adb_ok = (
            Path(adb_path).is_file()
            if Path(adb_path).parent != Path(".")
            else shutil.which(adb_path) is not None
        )
        results = {
            "adb": "可用" if adb_ok else "缺失",
            "driver": (
                "环境已配置" if os.environ.get("ADB_LIBUSB", "").strip() == "1" else "待检查"
            ),
            "database": "已初始化" if self.repository.path.is_file() else "不可用",
            "device": "已授权" if self._connected else "未连接",
            "wms": "会话有效" if self._verified_session_valid() else "未验证",
            "wms_version": self._current_wms_version()
            if self._wms_app_version
            else "未读取",
        }
        self.window.set_diagnostics(results)

    def close(self) -> None:
        self._stop_device_preview()
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
