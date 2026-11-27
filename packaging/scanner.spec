# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH).resolve().parent
migration_path = project_root / "migrations" / "001_initial.sql"
if not migration_path.is_file():
    raise FileNotFoundError(f"Required SQLite migration is missing: {migration_path}")

hiddenimports = collect_submodules("tt_auto_bag_scanner")
hiddenimports += [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "uiautomator2",
]

datas = [(str(migration_path), "migrations")]
datas += collect_data_files("tzdata")

a = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TT Auto Bag Scanner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TT Auto Bag Scanner",
)
