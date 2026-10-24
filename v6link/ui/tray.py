"""System tray presence: keep V6Link running while the window is hidden."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .. import APP_NAME, APP_TAGLINE
from .icons import icon, logo_pixmap
from .theme import C


class TrayIcon(QSystemTrayIcon):
    """Tray icon that restores the window and exposes an explicit quit action.

    The tray is only created when the desktop provides a system tray; callers
    must check :meth:`is_available` first and fall back to normal window
    behaviour otherwise.
    """

    show_requested = Signal()
    quit_requested = Signal()

    def __init__(self, window: QWidget):
        super().__init__(QIcon(logo_pixmap(64)), window)
        self._unread = 0
        self.setToolTip(self._tooltip())

        menu = QMenu(window)
        self.menu = menu
        self.show_action = menu.addAction(icon("chat", C.ACCENT, 15), "显示主窗口")
        self.show_action.triggered.connect(self.show_requested)
        menu.addSeparator()
        self.quit_action = menu.addAction(icon("close", C.DANGER, 15), "退出 V6Link")
        self.quit_action.triggered.connect(self.quit_requested)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)

    @staticmethod
    def is_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def _tooltip(self) -> str:
        if self._unread:
            return f"{APP_NAME} · {self._unread} 条未读消息"
        return f"{APP_NAME} · {APP_TAGLINE}"

    def set_unread(self, count: int) -> None:
        count = max(0, int(count))
        if count == self._unread:
            return
        self._unread = count
        self.setToolTip(self._tooltip())

    def notify(self, title: str, message: str) -> None:
        """Show a tray notification when the desktop supports them."""
        if not message or not self.supportsMessages():
            return
        self.showMessage(title, message, QSystemTrayIcon.Information, 4500)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_requested.emit()
