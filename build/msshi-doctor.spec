# -*- mode: python ; coding: utf-8 -*-
"""Windows GUI and console launchers sharing a single Python/Tk runtime."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ASSETS = ROOT / "assets"

a = Analysis(
    [str(ROOT / "src" / "msshi_doctor_app" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ASSETS / "msshi-doctor.ico"), "assets"),
        (str(ASSETS / "msshi-doctor-icon-64.png"), "assets"),
        (str(ASSETS / "msshi-doctor-banner-ui.png"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "scipy", "matplotlib", "PyQt5", "PySide6", "wx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

def launcher(name, *, console):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        upx=False,
        icon=str(ASSETS / "msshi-doctor.ico"),
    )


portable = COLLECT(
    launcher("MSSHI-Doctor", console=False),
    launcher("msshi-doctor-cli", console=True),
    a.binaries,
    a.datas,
    name="MSSHI-Doctor",
    upx=False,
)
