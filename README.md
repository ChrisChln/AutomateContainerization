# TT Auto Bag Scanner

Windows desktop application for importing an outbound parcel report, reviewing eligible parcels, verifying physical parcel ownership, and performing controlled single-sort scans through an iData K1 PDA.

## Development setup

Use Python 3.14 on Windows and a project virtual environment.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```

The application defaults to the isolated simulator. No carrier, sort-code, container, device serial, or shipment examples from the specification are preconfigured as production values.

## Current delivery state

The detailed scope and acceptance criteria are in [TT_Auto_Bag_Scanner_Development.md](TT_Auto_Bag_Scanner_Development.md). See [DEVELOPMENT_PROGRESS.md](DEVELOPMENT_PROGRESS.md) for verified implementation and test status.

