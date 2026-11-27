# Development Progress

## Current delivery

The offline V1 implementation is in place for the simulator, import workflow, local task history, device control layer, reports, and desktop UI. The light desktop layout follows the supplied Figma reference. No scan write was sent to a live PDA or WMS during this work.

This is a verified development build, not production acceptance. Live WMS behavior, site data, and clean-machine deployment still require controlled validation.

## Milestones

| Milestone | Status | Evidence |
|---|---|---|
| M0 PDA feasibility | Completed before this implementation | The supplied specification records V01–V07. Those earlier checks are not a production sign-off. |
| M1 project foundation | Complete | Python package, SQLite persistence, simulator, settings, and migrations are present. |
| M2 WMS controller | Offline implementation complete | XML fixture navigation, container verification, scan outcomes, duplicate handling, package checks, and failure guards have automated coverage. Live WMS result wording and XML shape remain unverified. |
| M3 Excel and business rules | Offline implementation complete | XLSX parsing, mapping, timezone selection, filtering, deduplication, sort-code review, and import preview/export are covered. Site report columns and timezone still need confirmation. |
| M4 task engine and SQLite | Offline implementation complete | State transitions, audit events, attempts, cancel/reconcile paths, recovery, backup, and restore have automated coverage. |
| M5 UI and reporting | Offline implementation complete | Main-window actions, run gates, pagination, statistics, import/export, and simulated workflow are covered by tests. |
| M6 Windows packaging and site acceptance | Onedir build complete; site acceptance pending | PyInstaller onedir output was generated on Windows 11 with Python 3.14. Clean-machine installation, driver setup, and controlled PDA/WMS acceptance have not been run. |

## Verification

- `.venv\Scripts\python.exe -m pytest -q` — 181 passed, exit code 0.
- `tests/integration/test_mode_isolation.py` — 5 passed; switching modes, starting/reviewing work, and changing device configuration are blocked while device verification is pending, and stale verification callbacks are ignored.
- `.venv\Scripts\ruff.exe check .` — passed.
- `.venv\Scripts\python.exe -m PyInstaller --noconfirm packaging/scanner.spec` — completed successfully.
- The onedir folder contains a 7,073,815-byte executable and `migrations/001_initial.sql` in PyInstaller's `_internal` directory.
- Packaged startup smoke test — main window opened and exited with code 0 using an isolated temporary data directory; both SQLite databases passed `quick_check` and recorded migration version 1. The temporary test data was removed.

## Live acceptance still required

- Confirm the live WMS hierarchy reports `com.ss.android.pda` for the app and visible page nodes. Unknown or untagged nodes are rejected by the detector.
- Confirm the exact successful-scan indicator. The controller currently requires the verified success wording and a matching latest tracking record; otherwise it records `UNKNOWN` and pauses. Do not loosen this rule without a controlled WMS check.
- Confirm all carrier aliases, sort codes, container assignments, report fields, timestamp formats, and report timezone.
- Confirm the manual physical-check attestation and its generated evidence reference meet the site's evidence procedure. The current UI does not capture a barcode or photo.
- Verify the PDA driver, device authorization, WMS version, and real device disconnect behavior on the target workstation and PDA.
- Complete the authorized single-parcel and duplicate-path tests, followed by reconciled small-batch acceptance. No live parcel was submitted during development.
- Confirm clean-machine installation, upgrade/rollback, supported Windows versions, retention, and throughput targets.
