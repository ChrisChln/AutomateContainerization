# TT Auto Bag Scanner Implementation Plan

> **For agentic workers:** Use subagent-driven development to implement these tasks. Keep task files disjoint, write and run a failing test before production code, and review each completed task against the specification.

**Goal:** Build the complete offline-first Windows TT Auto Bag Scanner specified in `TT_Auto_Bag_Scanner_Development.md`, covering all F01–F16 features and T01–T20 acceptance cases.

**Architecture:** Keep Qt UI, application services, domain rules, SQLite persistence, and PDA infrastructure separate. A single worker owns the selected device and serializes WMS operations. Real mode remains disabled by default, and each real write requires valid per-item physical evidence and an authorized start.

**Tech Stack:** Python 3.14, PySide6 Qt Widgets, uiautomator2/ADB, openpyxl, stdlib sqlite3, pytest, PyInstaller onedir.

---

## Requirements and invariants

- Implement the sixteen features F01–F16 and the twenty acceptance cases T01–T20 from the root specification.
- Keep all shipment numbers, device serials, container numbers, Logids, and XML fixtures synthetic or redacted. Do not use the real examples as defaults or replay them.
- No automatic container close, dispatch, inventory mutation, credential storage, production API calls, or retries of ambiguous writes.
- The default adapter is a simulator. Demo data and task history are isolated from real-mode records. Simulation performs zero ADB write calls.
- Persist `IN_FLIGHT` before sending one Enter. Treat transport loss, process failure, or unprovable results as `UNKNOWN`, pause, and require human reconciliation.
- A shipment cannot be submitted without an auditable physical check bound to that shipment and target container.
- Keep report timezone and carrier/sort mappings explicit. Do not infer unconfirmed values from the host clock or sample data.
- Store application data under `%LOCALAPPDATA%/TT-Auto-Bag-Scanner/`, outside the synchronized source tree.
- Keep product pages concise. Show labels and actionable status only, with no explanatory paragraphs.

## Shared code contracts

All implementers use `tt_auto_bag_scanner.domain.models` as the shared data contract.

- `PageState` contains `LOGIN`, `HOME`, `CONTAINER_MENU`, `SINGLE_EMPTY`, `BAG_READY`, `SCAN_RESULT`, `DUPLICATE_DIALOG`, and `UNKNOWN`.
- `TaskState` contains `DRAFT`, `READY`, `RUNNING`, `PAUSING`, `PAUSED`, `BLOCKED`, `COMPLETED`, and `STOPPED`.
- `ItemStatus` contains `WAIT_PHYSICAL`, `READY`, `IN_FLIGHT`, `SUCCESS`, `ALREADY_IN_BAG`, `UNKNOWN`, `REJECTED`, and `CANCELLED`.
- Immutable records include `DeviceInfo`, `XmlSnapshot`, `InputField`, `PageObservation`, `CarrierConfig`, `PhysicalCheck`, `QueueItem`, `VerifiedSession`, `ScanEvidence`, `ScanResult`, `RecoveryResult`, `ImportMapping`, `ImportSummary`, `TaskSnapshot`, and `TaskCounts`.
- `PageObservation` carries state, expected visible label values, focused input fields, latest visible tracking records, dialog text, Logid, and only redacted XML references.
- `VerifiedSession` binds one serial, warehouse, container, sort code, and session UUID. It becomes invalid after disconnect, application restart, manual takeover, or any unrecognized page.
- `ScanResult` distinguishes `SUCCESS`, `ALREADY_IN_BAG`, `UNKNOWN`, and `REJECTED`. Duplicate recovery state is recorded separately and can never increment new-success counts.
- The `DeviceAdapter` protocol exposes device listing, connection, app launch, XML snapshot, dynamic-bounds tap, text entry, key press, and read-only device metadata. The fake implementation exposes a write-call counter.
- `WmsController` exposes `open_bag(config)`, `submit_once(item, session)`, and `recover_duplicate(result, session)`. `submit_once` can send at most one Enter.
- `TaskRepository` exposes durable task creation, physical-check recording, `begin_attempt`, `finish_attempt`, state transitions, startup recovery, event history, and statistics.
- `ImportService.build_preview(...)` returns all source rows, excluded rows with reasons, conflicts, and deterministic candidate order. It never commits real scans.

## Task 1 — Project foundation and shared domain contracts

**Owner:** primary agent.

**Files:** `pyproject.toml`, `requirements.lock`, `.gitignore`, `AGENTS.md`, `README.md`, `DEVELOPMENT_PROGRESS.md`, `main.py`, `src/tt_auto_bag_scanner/domain/models.py`, `src/tt_auto_bag_scanner/domain/states.py`, `src/tt_auto_bag_scanner/domain/rules.py`, `src/tt_auto_bag_scanner/domain/device.py`, `src/tt_auto_bag_scanner/infrastructure/paths.py`, `tests/unit/test_models.py`, `tests/unit/test_state_transitions.py`.

- [ ] Write model and state-transition tests for illegal transitions, terminal-state counts, physical-evidence requirements, immutable task snapshots, and real-mode-off defaults.
- [ ] Run `python -m pytest tests/unit/test_models.py tests/unit/test_state_transitions.py -q` and confirm the tests fail because the contracts do not exist.
- [ ] Implement only the data records, validation rules, device protocol, safe data-directory resolution, and package entry point needed by those tests.
- [ ] Run the same command and confirm every new test passes.
- [ ] Pin dependencies only after a clean virtual environment can install and import the full UI, PDA, Excel, test, and packaging stack on Windows Python 3.14.

## Task 2 — PDA adapter, WMS state detector, and guarded controller

**Owner:** device subagent. Depends on Task 1 contracts.

**Files:** `src/tt_auto_bag_scanner/infrastructure/device_manager.py`, `src/tt_auto_bag_scanner/infrastructure/adb_adapter.py`, `src/tt_auto_bag_scanner/infrastructure/fake_device.py`, `src/tt_auto_bag_scanner/infrastructure/state_detector.py`, `src/tt_auto_bag_scanner/infrastructure/wms_controller.py`, `src/tt_auto_bag_scanner/infrastructure/exception_handler.py`, `tests/unit/test_device_manager.py`, `tests/unit/test_state_detector.py`, `tests/unit/test_wms_controller.py`, `tests/fixtures/wms/`.

- [ ] Add failing tests for device states, two-device selection, app and warehouse verification, XML page classes, dynamic focused inputs, GOFO/VEHO synthetic switching, one-Enter maximum, duplicate recognition, and fail-closed unknown pages.
- [ ] Verify RED with `python -m pytest tests/unit/test_device_manager.py tests/unit/test_state_detector.py tests/unit/test_wms_controller.py -q`.
- [ ] Implement production ADB access and FakeDeviceAdapter separately. The application selects FakeDeviceAdapter unless explicit real-mode authorization is passed.
- [ ] Use sanitized synthetic XML fixtures. Do not put user shipment IDs, serials, cookies, credentials, or real WMS screenshots into the repository.
- [ ] Re-run the focused tests and confirm RED-to-GREEN. Hardware tests remain opt-in and are not run during this implementation.

## Task 3 — Excel import, filtering, carrier mapping, physical checks, and exports

**Owner:** data subagent. Depends on Task 1 contracts.

**Files:** `src/tt_auto_bag_scanner/infrastructure/excel_parser.py`, `src/tt_auto_bag_scanner/infrastructure/filter_engine.py`, `src/tt_auto_bag_scanner/infrastructure/carrier_config.py`, `src/tt_auto_bag_scanner/infrastructure/physical_check_service.py`, `src/tt_auto_bag_scanner/infrastructure/exporter.py`, `src/tt_auto_bag_scanner/application/import_service.py`, `tests/unit/test_excel_parser.py`, `tests/unit/test_filter_engine.py`, `tests/unit/test_carrier_config.py`, `tests/unit/test_physical_check_service.py`, `tests/unit/test_exporter.py`, `tests/fixtures/xlsx/`.

- [ ] Add failing tests for XLSX-only read mode, source hash preservation, field mapping, formula cache absence, damaged files, leading-zero and long text shipment IDs, strict status equality, inclusive threshold boundary, ambiguous DST/future/missing times, deterministic deduplication, cross-carrier conflicts, multi-sort ambiguity, snapshot freezing, physical-check invalidation, and CSV formula injection.
- [ ] Verify RED with the focused parser/filter/config/evidence/export test files.
- [ ] Implement import and preview from synthetic fixtures. Require an explicitly chosen report timezone before time filtering. Keep the suggested 30-minute value visible as a setting that the operator confirms.
- [ ] Never default carrier mappings or containers from historical test values. Keep unresolved rows outside the executable queue.
- [ ] Re-run the focused tests and confirm RED-to-GREEN.

## Task 4 — SQLite repository, task worker, and crash recovery

**Owner:** persistence subagent. Depends on Task 1 contracts and Tasks 2–3 public protocols.

**Files:** `migrations/001_initial.sql`, `src/tt_auto_bag_scanner/infrastructure/repository.py`, `src/tt_auto_bag_scanner/application/task_service.py`, `src/tt_auto_bag_scanner/application/scan_worker.py`, `src/tt_auto_bag_scanner/application/startup_recovery.py`, `tests/unit/test_repository.py`, `tests/unit/test_task_service.py`, `tests/unit/test_scan_worker.py`, `tests/integration/test_crash_recovery.py`.

- [ ] Add failing tests for schema constraints, same-task shipment uniqueness, atomic `IN_FLIGHT` before adapter write, outcomes persisted with audit events, `UNKNOWN` after interruption, no resend after ambiguous write, safe pause/stop boundaries, restart recovery, backup/restore, and physical evidence invalidation.
- [ ] Verify RED with the focused repository, task-service, worker, and crash-recovery test files.
- [ ] Implement SQLite WAL with per-thread connections, a serialized write service, schema versions, transactional state changes, and explicit manual-reconciliation events.
- [ ] Ensure the worker never starts on unresolved `IN_FLIGHT`, `UNKNOWN`, or incomplete duplicate recovery and never closes a container.
- [ ] Re-run the focused tests and confirm RED-to-GREEN.

## Task 5 — Desktop UI, diagnostics, history, and application wiring

**Owner:** primary agent. Depends on Tasks 1–4 public APIs.

**Files:** `src/tt_auto_bag_scanner/ui/main_window.py`, `src/tt_auto_bag_scanner/ui/table_models.py`, `src/tt_auto_bag_scanner/ui/carrier_settings.py`, `src/tt_auto_bag_scanner/ui/history.py`, `src/tt_auto_bag_scanner/ui/diagnostics.py`, `src/tt_auto_bag_scanner/app.py`, `main.py`, `tests/ui/test_main_window.py`, `tests/integration/test_simulated_application.py`.

- [ ] Add failing offscreen Qt tests for task/config/history/settings navigation, disabled start gates, one-worker ownership, UI responsiveness while a worker runs, pause/stop state reflection, DEMO isolation, and real-mode-off behavior.
- [ ] Verify RED with `QT_QPA_PLATFORM=offscreen python -m pytest tests/ui tests/integration/test_simulated_application.py -q`.
- [ ] Build a concise Qt Widgets interface with device/warehouse status, task queue, carrier/sort/container configuration, per-item physical confirmation, controls, statistics, and logs. Use labels and status text only, with no explanatory paragraphs.
- [ ] Connect UI to application services by signals and slots. Never let UI code call ADB or SQLite directly.
- [ ] Keep a start action disabled until the selected task, mappings, device mode, and physical checks satisfy the spec. The UI cannot enable automatic container close or production API calls.
- [ ] Re-run the UI and integration tests and confirm RED-to-GREEN.

## Task 6 — Reports, packaging, installation, and recovery documentation

**Owner:** primary agent, with package review from subagents.

**Files:** `packaging/scanner.spec`, `README.md`, `DEVELOPMENT_PROGRESS.md`, `tests/integration/test_packaging_configuration.py`.

- [ ] Add failing configuration tests for onedir, Windows target, data-directory separation, retained history on uninstall/upgrade, and packaged Qt plugin discovery.
- [ ] Verify RED with `python -m pytest tests/integration/test_packaging_configuration.py -q`.
- [ ] Create onedir PyInstaller configuration and startup diagnostics for ADB path, LibUSB, database write access, device authorization, and WMS package version.
- [ ] Document installation, manual WMS login, simulation, explicit real-mode authorization, backups, upgrade, rollback, and hardware smoke-test steps. Do not claim a clean-machine test that has not been run.
- [ ] Re-run the focused tests and build the Windows onedir application when dependencies are available.

## Task 7 — Requirement-by-requirement verification

**Owner:** primary agent with independent reviewers.

- [ ] Run the full offline suite and lint command from `pyproject.toml`; record the exact command and result in `DEVELOPMENT_PROGRESS.md`.
- [ ] Map each T01–T20 to tests and mark hardware-only checks as unverified until performed with explicit site authorization and controlled physical parcels.
- [ ] Review the complete implementation against F01–F16, forbidden operations, persistence invariants, timezone handling, and no-explanatory-copy UI rule.
- [ ] Review the final Git diff and ensure no secrets, real shipment IDs, real device serials, accidental data files, or generated build artifacts are tracked.
- [ ] Report remaining unverified deployment and WMS acceptance gates without describing them as passed.

## Execution order and parallel ownership

Complete Task 1 first because every subsystem shares its contracts. Then run Tasks 2, 3, and 4 concurrently, each in its assigned file set. Task 5 integrates only the published APIs from those tasks. Task 6 and the final verification follow after integration. The primary agent reviews each module against the spec before merging its API into the application.

## Deferred site confirmations

The application must support explicit choices for report timezone, report headers, full carrier/sort list, and operator identity because the source specification leaves them unresolved. They remain configuration gates and are not fabricated from sample values. Real WMS writes, physical parcel checks, long-duration throughput, and production sign-off are site acceptance work and are not performed by this development run.
