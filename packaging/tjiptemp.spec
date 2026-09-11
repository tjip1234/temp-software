# PyInstaller spec — one bundle, three shapes: the Linux AppImage payload, the
# macOS .app inside the .dmg, and the Windows .exe.
#
#     pyinstaller packaging/tjiptemp.spec --noconfirm
#
# The platform build scripts call this; run it directly only to debug a bundle.
# Expect roughly 150 MB before compression: Qt, numpy, scipy, pandas and
# matplotlib are all large, and excluding their unused corners (below) claws
# back a good fraction of it.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def icon_for_platform():
    """The icon to embed in the executable, or None if there is none to embed.

    Only Windows and macOS carry an icon inside the binary; on Linux the icon
    belongs to the AppDir, which the AppImage script fills from the hicolor PNGs,
    so passing one here only earns a warning.

    ``packaging/make_icons.py`` writes all the formats and every build script
    runs it first. Returning None rather than a path that might not exist
    matters: PyInstaller aborts the entire build over an icon it cannot open,
    and a missing icon is not a reason to have no application.
    """
    if IS_MAC:
        name = "icon.icns"
    elif IS_WINDOWS:
        name = "icon.ico"
    else:
        return None
    path = ROOT / "packaging" / name
    return str(path) if path.exists() else None


ICON = icon_for_platform()


def project_version() -> str:
    """Read the version from pyproject rather than restating it here.

    Three copies of "0.1.0" in the Info.plist was three chances to ship a bundle
    whose About box disagrees with the package it came from.
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


VERSION = project_version()

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
    # A launcher that imports the package, not the package's own __main__:
    # PyInstaller runs the entry script as a top-level module, where the
    # relative imports inside tjiptemp/__main__.py cannot resolve.
    [str(ROOT / "packaging" / "entrypoint.py")],
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
    # The builder's own architecture, on every platform. A universal2 Mac app
    # needs every compiled wheel to be universal2 too, and PyPI's are not:
    # Pillow's _avif and numpy ship one architecture each, so the first CI
    # build stopped with "is not a fat binary". macos-latest is Apple Silicon.
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
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
        icon=ICON,
        bundle_identifier="io.github.tjiptemp",
        version=VERSION,
        info_plist={
            "CFBundleName": "TjipTemp",
            "CFBundleDisplayName": "TjipTemp",
            "CFBundleShortVersionString": VERSION,
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
