# -*- mode: python ; coding: utf-8 -*-
# Reproducible one-file build for the Windows desktop client.
# Run from the repository root:
#   .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean V6Link.spec

hiddenimports = [
    "cryptography.hazmat.bindings._rust",
    "cryptography.hazmat.primitives.kdf.hkdf",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cryptography.hazmat.primitives.asymmetric.x25519",
    "cryptography.hazmat.primitives.ciphers.aead",
    "psutil",
    "PySide6.QtSvg",
]

# Modules that V6Link never imports. Keeping them out substantially reduces
# the one-file bundle without affecting Qt Widgets/Qt SVG behavior.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick3D",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtStateMachine",
    "PySide6.QtSensors",
    "PySide6.QtTextToSpeech",
    "PySide6.QtNetworkAuth",
    "PySide6.QtScxml",
    "PySide6.QtSpatialAudio",
    "tkinter",
    "unittest",
    "doctest",
    "pydoc",
    "pytest",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="V6Link",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/V6Link.ico",
    version="packaging/version_info.txt",
)
