"""Application entry point for the V6Link desktop client."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import logging
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import APP_NAME, __version__
from ..errors import VaultError
from ..keyvault import default_data_dir
from ..runtime import BackendRuntime
from ..updates import cleanup_on_start, is_frozen, running_executable
from ..vault import Vault
from .bridge import EventBridge
from .icons import logo_pixmap
from .main_window import MainWindow
from .theme import apply_theme


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="v6link",
        description="V6Link — 端到端加密的 IPv6 点对点通讯工具",
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="覆盖本地加密数据目录")
    parser.add_argument("--port", type=int, default=None, help="覆盖本次启动的 IPv6 监听端口")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="输出运行环境诊断信息后退出（用于排查单文件打包路径）",
    )
    return parser.parse_args(argv)


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("V6Link.Desktop.0.1")
    except Exception:
        pass


def _fatal(message: str) -> int:
    box = QMessageBox()
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle("V6Link 无法启动")
    box.setText(message)
    box.exec()
    return 1


def _diagnose() -> int:
    """Print runtime paths so one-file builds can be debugged from a console."""
    payload = {
        "version": __version__,
        "platform": sys.platform,
        "frozen": is_frozen(),
        "sys.executable": sys.executable,
        "sys._MEIPASS": getattr(sys, "_MEIPASS", None),
        "persisted_executable": str(running_executable() or ""),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with contextlib.suppress(Exception):
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    with contextlib.suppress(OSError):
        (Path(tempfile.gettempdir()) / "v6link-diagnose.json").write_text(
            text, encoding="utf-8"
        )
    return 0


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    app = QApplication([sys.argv[0]])
    # 主窗口隐藏到系统托盘后仍需接收消息。真正退出只由关闭窗口或托盘菜单触发。
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("V6Link")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(logo_pixmap(64)))
    apply_theme(app)
    _set_windows_app_id()

    data_dir = args.data_dir.expanduser().resolve() if args.data_dir else default_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _fatal(f"无法创建数据目录：\n{data_dir}\n\n{exc}")
    # A previous peer-pushed update may have left a swap script or partial
    # download behind; neither is needed once this instance is starting.
    with contextlib.suppress(Exception):
        cleanup_on_start(data_dir)

    lock = QLockFile(str(data_dir / "app.lock"))
    lock.setStaleLockTime(5000)
    if not lock.tryLock(150):
        return _fatal(
            "V6Link 已在本机运行。\n"
            "请双击系统托盘中的 V6Link 图标恢复窗口，或先退出旧进程。"
        )

    try:
        vault = Vault(data_dir)
    except VaultError as exc:
        lock.unlock()
        return _fatal(f"无法解锁本地加密保险库：\n\n{exc}")
    except Exception as exc:
        lock.unlock()
        logging.exception("vault initialization failed")
        return _fatal(f"初始化本地数据失败：\n\n{exc}")

    bridge = EventBridge()
    runtime = BackendRuntime(
        vault,
        bridge.emit_event,
        listen_port=args.port,
    )
    try:
        runtime.start(timeout=20.0)
    except Exception as exc:
        bridge.detach()
        runtime.stop(timeout=2.0)
        lock.unlock()
        return _fatal(f"后端服务启动失败：\n\n{exc}")

    window = MainWindow(runtime, bridge)
    window.show()
    try:
        exit_code = app.exec()
    finally:
        bridge.detach()
        runtime.mute_events()
        runtime.stop(timeout=8.0)
        lock.unlock()
    return int(exit_code)


def main() -> int:
    if "--diagnose" in sys.argv[1:]:
        return _diagnose()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run()

