# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

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

# Pin and verify the official Windows x64 release before adding it to the bundle.
scrcpy_release = "scrcpy-win64-v5.0.1"
scrcpy_checksum = "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5"
scrcpy_url = f"https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/{scrcpy_release}.zip"
scrcpy_cache = project_root / "build" / "scrcpy" / f"{scrcpy_release}.zip"
if scrcpy_cache.is_file():
    scrcpy_archive = scrcpy_cache.read_bytes()
else:
    with urlopen(scrcpy_url, timeout=30) as response:
        scrcpy_archive = response.read()
if sha256(scrcpy_archive).hexdigest() != scrcpy_checksum:
    raise ValueError(f"scrcpy archive failed SHA-256 verification: {scrcpy_cache}")
if not scrcpy_cache.is_file():
    scrcpy_cache.parent.mkdir(parents=True, exist_ok=True)
    scrcpy_cache.write_bytes(scrcpy_archive)
with ZipFile(BytesIO(scrcpy_archive)) as archive:
    archive.extractall(scrcpy_cache.parent)
datas.append((str(scrcpy_cache.parent / scrcpy_release), "tools/scrcpy"))

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
