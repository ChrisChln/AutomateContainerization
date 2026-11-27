from __future__ import annotations

from tt_auto_bag_scanner.domain.models import DeviceMode
from tt_auto_bag_scanner.infrastructure.paths import app_data_dir, database_path, settings_path


def test_application_data_paths_are_under_local_app_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    data_dir = app_data_dir()

    assert data_dir == (tmp_path / "Local" / "TT-Auto-Bag-Scanner").resolve()
    assert database_path(DeviceMode.SIMULATED) != database_path(DeviceMode.REAL)
    assert database_path(DeviceMode.SIMULATED).parent == data_dir
    assert settings_path().parent == data_dir


def test_path_resolution_does_not_create_the_application_directory(monkeypatch, tmp_path) -> None:
    target = tmp_path / "local-data"
    monkeypatch.setenv("LOCALAPPDATA", str(target))

    resolved = app_data_dir()

    assert resolved == (target / "TT-Auto-Bag-Scanner").resolve()
    assert not resolved.exists()
