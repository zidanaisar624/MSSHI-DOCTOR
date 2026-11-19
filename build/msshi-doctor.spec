# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for standalone MSSHI Doctor Windows desktop executable."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ASSETS = ROOT / "assets"

a = Analysis(
    [str(ROOT / "src" / "msshi_doctor_app" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ASSETS / "msshi-doctor.ico"), "assets"),
        (str(ASSETS / "msshi-doctor-icon.png"), "assets"),
        (str(ASSETS / "msshi-doctor-icon-256.png"), "assets"),
        (str(ASSETS / "msshi-doctor-icon-64.png"), "assets"),
        (str(ASSETS / "msshi-doctor-banner-ui.png"), "assets"),
        (str(ASSETS / "msshi-doctor-banner.jpg"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "scipy", "matplotlib", "PyQt5", "PySide6", "wx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MSSHI-Doctor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ASSETS / "msshi-doctor.ico"),
)
