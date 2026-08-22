# PyInstaller spec — the macOS .app and the Windows .exe.
#
# Linux users get a distro package instead (AUR / DEB), which is smaller, uses
# system Qt, and integrates with the desktop properly. Bundling is for the two
# platforms without a package manager to lean on.
#
#     pyinstaller packaging/tjiptemp.spec --noconfirm
#
# Expect roughly 150 MB before compression: Qt, numpy, scipy, pandas and
# matplotlib are all large, and excluding their unused corners (below) claws
# back a good fraction of it.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
IS_MAC = sys.platform == "darwin"

hidden = [
    # Uvicorn and FastAPI resolve these by name at runtime, so static analysis
    # cannot see them.
    *collect_submodules("uvicorn"),
    "uvicorn.logging",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.off",
    # Qt platform plugins and the serial backend for this OS.
    "PySide6.QtSvg",
    "serial.tools.list_ports",
    # zeroconf picks its implementation at import time.
    *collect_submodules("zeroconf"),
]

datas = [
    (str(ROOT / "docs" / "protocol.md"), "docs"),
    (str(ROOT / "firmware-ref" / "tjip_proto.h"), "firmware-ref"),
    (str(ROOT / "firmware-ref" / "tjip_proto.c"), "firmware-ref"),
    *collect_data_files("pyqtgraph"),
]

excludes = [
    # Nothing here imports these, and each is tens of megabytes.
    "tkinter", "PyQt5", "PyQt6", "PySide2", "IPython", "jupyter", "notebook",
    "pytest", "sphinx", "setuptools", "pip",
    "matplotlib.tests", "numpy.tests", "scipy.tests", "pandas.tests",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtQuick3D", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSql",
]

a = Analysis(
    [str(ROOT / "src" / "tjiptemp" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tjiptemp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX and macOS code signing do not get along
    console=False,      # a GUI app; --headless users run the module directly
    argv_emulation=IS_MAC,
    target_arch="universal2" if IS_MAC else None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "icon.icns") if IS_MAC
         else str(ROOT / "packaging" / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tjiptemp",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="TjipTemp.app",
        icon=str(ROOT / "packaging" / "icon.icns"),
        bundle_identifier="io.github.tjiptemp",
        version="0.1.0",
        info_plist={
            "CFBundleName": "TjipTemp",
            "CFBundleDisplayName": "TjipTemp",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # macOS 13+ prompts for this the first time a serial port is opened.
            "NSUSBUsageDescription":
                "TjipTemp talks to your thermometer board over its USB serial port.",
            "NSBluetoothAlwaysUsageDescription":
                "TjipTemp can find and configure thermometer boards over Bluetooth LE.",
            "NSLocalNetworkUsageDescription":
                "TjipTemp finds thermometer boards on your local network.",
            "NSBonjourServices": ["_tjiptemp._tcp"],
        },
    )
