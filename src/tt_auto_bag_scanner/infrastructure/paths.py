"""Resolve persistent data paths outside the synchronized source tree."""

from __future__ import annotations

import os
from pathlib import Path

from tt_auto_bag_scanner.domain.models import DeviceMode

APP_DIRECTORY_NAME = "TT-Auto-Bag-Scanner"


def app_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (base / APP_DIRECTORY_NAME).resolve()


def database_path(mode: DeviceMode) -> Path:
    filename = "simulated.sqlite3" if mode is DeviceMode.SIMULATED else "real.sqlite3"
    return app_data_dir() / filename


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def ensure_app_data_dir() -> Path:
    path = app_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
