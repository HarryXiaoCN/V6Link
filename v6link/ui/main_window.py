"""Main V6Link window and event orchestration."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..netutil import format_endpoint
from ..runtime import BackendRuntime
from .dialogs import (
    ContactDialog,
    FileOfferDialog,
    MyCardDialog,
    PendingRequestsDialog,
    SettingsDialog,
    UpdateOfferDialog,
)
from .icons import icon, logo_pixmap
from .theme import C
from .tray import TrayIcon
from .widgets import (
    ChatPage,
    Sidebar,
    WelcomePage,
    _message_preview,
)

log = logging.getLogger("v6link.ui")


class ToastItem(QFrame):
    def __init__(self, message: str, level: str = "info", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toastFrame")
        self.setProperty("level", level)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedWidth(430)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 10, 14, 10)
        layout.setSpacing(9)
        icon_name = {"error": "alert", "warning": "alert", "success": "check", "info": "info"}.get(level, "info")
        color = {"error": C.DANGER, "warning": C.WARNING, "success": C.SUCCESS, "info": C.ACCENT}.get(level, C.ACCENT)
        icon_label = QLabel()
        icon_label.setPixmap(icon(icon_name, color, 17).pixmap(QSize(17, 17)))
        icon_label.setFixedWidth(18)
        layout.addWidget(icon_label, 0, Qt.AlignTop)
        text = QLabel(message)
        text.setTextFormat(Qt.PlainText)
        text.setObjectName("toastText")
        text.setWordWrap(True)
        text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(text, 1)
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(0.0)
        self.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b"opacity", self)
        self.animation.setDuration(160)

    def appear(self) -> None:
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.start()


class ToastHost(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 0, 30, 108)
        layout.setSpacing(7)
        layout.addStretch(1)
        self.row = QHBoxLayout()
        self.row.addStretch(1)
        self.stack = QVBoxLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(7)
        self.row.addLayout(self.stack)
        self.row.addStretch(1)
        layout.addLayout(self.row)
        self.items: list[ToastItem] = []

    def show_message(self, message: str, level: str = "info", duration: int = 4200) -> None:
        if not message:
            return
        item = ToastItem(message, level, self)
        self.stack.addWidget(item, 0, Qt.AlignHCenter)
        item.show()
        item.adjustSize()
        item.appear()
        self.items.append(item)
        while len(self.items) > 3:
            oldest = self.items.pop(0)
            self.stack.removeWidget(oldest)
            oldest.deleteLater()
        QTimer.singleShot(duration, lambda: self._dismiss(item))

    def _dismiss(self, item: ToastItem) -> None:
        if item not in self.items:
            return
        self.items.remove(item)
        item.animation.stop()
        item.animation.setDuration(180)
        item.animation.setStartValue(item.effect.opacity())
        item.animation.setEndValue(0.0)
        try:
            item.animation.finished.disconnect()
        except RuntimeError:
            pass
        item.animation.finished.connect(item.deleteLater)
        item.animation.start()

    def resize_to_parent(self) -> None:
        if self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())
            self.raise_()


class MainWindow(QMainWindow):
    def __init__(self, runtime: BackendRuntime, bridge, parent: QWidget | None = None):
        super().__init__(parent)
        self.runtime = runtime
        self.bridge = bridge
        self._shutting_down = False
        snapshot = runtime.snapshot()
        self.profile = snapshot.get("profile", {})
        self.settings = snapshot.get("settings", {})
        self.peer_id = snapshot.get("peer_id", "")
        self.contacts: dict[str, dict[str, Any]] = {
            contact["id"]: contact for contact in snapshot.get("contacts", [])
        }
        self.pending_requests: list[dict[str, Any]] = list(snapshot.get("pending_requests", []))
        self.network = snapshot.get("network", {"listening": False, "port": 0})
        self.unread: dict[str, int] = {}
        self.latest: dict[str, dict[str, Any] | None] = {}
        self.selected_id = ""
        self._file_offer_queue: list[dict[str, Any]] = []
        self._file_offer_dialog: FileOfferDialog | None = None
        self._update_dialogs: dict[str, UpdateOfferDialog] = {}
        self.tray: TrayIcon | None = None
        self._tray_hint_shown = False
        self._restore_maximized = False
        self._load_latest_messages()
        self.setWindowTitle(f"V6Link {__version__}")
        self.setWindowIcon(QIcon(logo_pixmap(64)))
        self.resize(1220, 800)
        self.setMinimumSize(1040, 680)
        self._build_ui()
        self._connect_signals()
        self._setup_tray()
        self._apply_network_state()
        self._refresh_contacts()
        self._refresh_requests()
        self._refresh_profile()
        if self.contacts:
            initial = self._sorted_contacts()[0]["id"]
            self.select_contact(initial)
        else:
            self._show_welcome()
        bridge.attach(self.handle_event)
        QTimer.singleShot(0, self._late_init)

    # ----------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_rail())
        self.sidebar = Sidebar()
        outer.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.network_banner = self._build_network_banner()
        banner_wrap = QWidget()
        self._banner_wrap = banner_wrap
        banner_layout = QVBoxLayout(banner_wrap)
        banner_layout.setContentsMargins(18, 10, 18, 0)
        banner_layout.setSpacing(0)
        banner_layout.addWidget(self.network_banner)
        right_layout.addWidget(banner_wrap)
        self.stack = QStackedWidget()
        self.welcome = WelcomePage()
        self.chat = ChatPage()
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.chat)
        right_layout.addWidget(self.stack, 1)
        outer.addWidget(right, 1)
        self.toasts = ToastHost(root)
        self.toasts.resize_to_parent()

    def _build_rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("rail")
        rail.setFixedWidth(78)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 20, 0, 17)
        layout.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(40))
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedHeight(46)
        logo.setToolTip("V6Link · IPv6 加密直连通讯")
        layout.addWidget(logo)
        layout.addSpacing(16)

        self.nav_chat = QToolButton()
        self.nav_chat.setObjectName("railButton")
        self.nav_chat.setCheckable(True)
        self.nav_chat.setChecked(True)
        self.nav_chat.setCursor(Qt.PointingHandCursor)
        self.nav_chat.setFixedSize(44, 44)
        self.nav_chat.setIcon(icon("chat", C.ACCENT, 21))
        self.nav_chat.setIconSize(QSize(21, 21))
        self.nav_chat.setToolTip("消息")
        layout.addWidget(self.nav_chat, 0, Qt.AlignHCenter)

        self.nav_requests = QToolButton()
        self.nav_requests.setObjectName("railButton")
        self.nav_requests.setCursor(Qt.PointingHandCursor)
        self.nav_requests.setFixedSize(44, 44)
        self.nav_requests.setIcon(icon("users", C.MUTED, 20))
        self.nav_requests.setIconSize(QSize(20, 20))
        self.nav_requests.setToolTip("好友请求")
        layout.addWidget(self.nav_requests, 0, Qt.AlignHCenter)

        self.nav_card = QToolButton()
        self.nav_card.setObjectName("railButton")
        self.nav_card.setCursor(Qt.PointingHandCursor)
        self.nav_card.setFixedSize(44, 44)
        self.nav_card.setIcon(icon("card", C.MUTED, 20))
        self.nav_card.setIconSize(QSize(20, 20))
        self.nav_card.setToolTip("我的加密名片")
        layout.addWidget(self.nav_card, 0, Qt.AlignHCenter)

        layout.addStretch(1)
        self.rail_lock = QLabel()
        self.rail_lock.setPixmap(icon("shield", "#34544F", 17).pixmap(QSize(17, 17)))
        self.rail_lock.setToolTip("端到端加密 · AES-256-GCM")
        self.rail_lock.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.rail_lock)
        self.nav_settings = QToolButton()
        self.nav_settings.setObjectName("railButton")
        self.nav_settings.setCursor(Qt.PointingHandCursor)
        self.nav_settings.setFixedSize(44, 44)
        self.nav_settings.setIcon(icon("settings", C.MUTED, 20))
        self.nav_settings.setIconSize(QSize(20, 20))
        self.nav_settings.setToolTip("设置")
        layout.addWidget(self.nav_settings, 0, Qt.AlignHCenter)
        layout.addSpacing(2)
        self.network_dot = QLabel()
        self.network_dot.setFixedSize(7, 7)
        self.network_dot.setStyleSheet("background: #3DDFC0; border-radius: 3px;")
        layout.addWidget(self.network_dot, 0, Qt.AlignHCenter)
        self.network_dot_tooltip = "IPv6 监听中"
        self.network_dot.setToolTip(self.network_dot_tooltip)
        return rail

    def _build_network_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("networkBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(13, 9, 10, 9)
        layout.setSpacing(9)
        alert_icon = QLabel()
        alert_icon.setPixmap(icon("wifi_off", C.WARNING, 17).pixmap(QSize(17, 17)))
        layout.addWidget(alert_icon)
        self.network_banner_text = QLabel(
            "IPv6 监听未启动。你仍然可以主动连接好友，但好友无法主动联系你。"
        )
        self.network_banner_text.setWordWrap(True)
        self.network_banner_text.setStyleSheet("font-size: 10px; color: #D8BE8E;")
        layout.addWidget(self.network_banner_text, 1)
        fix = QPushButton("打开设置")
        fix.setObjectName("ghostButton")
        fix.setCursor(Qt.PointingHandCursor)
        fix.clicked.connect(self._open_settings)
        layout.addWidget(fix)
        banner.hide()
        return banner

    def _connect_signals(self) -> None:
        self.sidebar.add_contact_requested.connect(self._open_add_contact)
        self.sidebar.friend_selected.connect(self.select_contact)
        self.sidebar.requests_clicked.connect(self._open_requests)
        self.sidebar.self_clicked.connect(self._open_my_card)
        self.nav_requests.clicked.connect(self._open_requests)
        self.nav_card.clicked.connect(self._open_my_card)
        self.nav_settings.clicked.connect(self._open_settings)
        self.nav_chat.clicked.connect(lambda: self.chat.focus_composer() if self.selected_id else None)
        self.welcome.add_contact_requested.connect(self._open_add_contact)
        self.welcome.card_requested.connect(self._open_my_card)
        self.welcome.settings_requested.connect(self._open_settings)
        self.chat.send_text.connect(self._send_text)
        self.chat.attach_requested.connect(self._choose_files)
        self.chat.files_dropped.connect(self._send_files)
        self.chat.more_clicked.connect(self._open_current_menu)
        self.chat.open_file_requested.connect(self._open_file)
        self.chat.export_file_requested.connect(self._export_file)
        self.chat.retry_requested.connect(self._retry_message)
        self.chat.cancel_requested.connect(self._cancel_transfer)
        self.chat.reveal_requested.connect(self._reveal_file)

    def _late_init(self) -> None:
        self.toasts.resize_to_parent()
        self.toasts.raise_()
        if self.selected_id:
            self.chat.focus_composer()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "toasts"):
            self.toasts.resize_to_parent()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32":
            try:
                hwnd = int(self.winId())
                value = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            except Exception:
                pass

    # ------------------------------------------------------------- view models

    def _load_latest_messages(self) -> None:
        for contact_id in self.contacts:
            messages = self.runtime.vault.list_messages(contact_id)
            self.latest[contact_id] = messages[-1] if messages else None

    def _sorted_contacts(self) -> list[dict[str, Any]]:
        def key(contact: dict[str, Any]) -> float:
            message = self.latest.get(contact.get("id", ""))
            if message:
                return float(message.get("created_at", 0.0) or 0.0)
            return float(contact.get("created_at", 0.0) or contact.get("last_seen", 0.0) or 0.0)

        return sorted(self.contacts.values(), key=key, reverse=True)

    def _refresh_contacts(self) -> None:
        rows = []
        for contact in self._sorted_contacts():
            contact_id = contact.get("id", "")
            latest = self.latest.get(contact_id)
            rows.append(
                {
                    "contact": contact,
                    "preview": _message_preview(latest, contact),
                    "timestamp": float(latest.get("created_at", 0.0) or 0.0)
                    if latest
                    else float(contact.get("created_at", 0.0) or 0.0),
                    "unread": int(self.unread.get(contact_id, 0)),
                }
            )
        self.sidebar.set_friends(rows, self.selected_id)
        if self.tray is not None:
            self.tray.set_unread(sum(self.unread.values()))

    def _refresh_requests(self) -> None:
        self.sidebar.set_requests(self.pending_requests)
        if self.pending_requests:
            self.nav_requests.setIcon(icon("users", C.ACCENT, 20))
            self.nav_requests.setToolTip(f"好友请求 ({len(self.pending_requests)})")
        else:
            self.nav_requests.setIcon(icon("users", C.MUTED, 20))
            self.nav_requests.setToolTip("好友请求")

    def _refresh_profile(self) -> None:
        self.sidebar.set_profile(
            self.profile.get("display_name", "我"),
            self.peer_id,
            bool(self.network.get("listening")),
            int(self.network.get("port", 0) or 0),
        )
        self.welcome.set_network(bool(self.network.get("listening")), int(self.network.get("port", 0) or 0))
        self.chat.set_network(bool(self.network.get("listening")), int(self.network.get("port", 0) or 0))

    def _apply_network_state(self) -> None:
        listening = bool(self.network.get("listening"))
        port = int(self.network.get("port", 0) or 0)
        self.network_banner.setVisible(not listening)
        if hasattr(self, "_banner_wrap"):
            self._banner_wrap.setVisible(not listening)
        if listening:
            self.network_dot.setStyleSheet("background: #3DDFC0; border-radius: 3px;")
            self.network_dot.setToolTip(f"IPv6 监听中 · 端口 {port}")
            self.network_banner_text.setText("")
        else:
            self.network_dot.setStyleSheet("background: #F3B65C; border-radius: 3px;")
            self.network_dot.setToolTip("IPv6 监听未启动")
            self.network_banner_text.setText(
                "IPv6 监听未启动。你仍然可以主动连接好友，但好友无法主动联系你。"
            )
        self._refresh_profile()
        self._refresh_contacts()

    def _show_welcome(self) -> None:
        self.selected_id = ""
        self.sidebar.set_selected("")
        self.stack.setCurrentWidget(self.welcome)
        self.setWindowTitle(f"V6Link {__version__}")

    def select_contact(self, contact_id: str) -> None:
        contact = self.contacts.get(contact_id)
        if contact is None:
            return
        self.selected_id = contact_id
        self.unread[contact_id] = 0
        self.sidebar.set_selected(contact_id)
        self.stack.setCurrentWidget(self.chat)
        self.chat.set_contact(contact)
        messages = self.runtime.vault.list_messages(contact_id)
        self.chat.load_messages(messages)
        self._refresh_contacts()
        self.nav_chat.setChecked(True)
        self.setWindowTitle(f"V6Link {__version__} · {contact.get('name', '好友')}")
        QTimer.singleShot(30, self.chat.focus_composer)

    # ------------------------------------------------------------------ events

    def handle_event(self, event: dict[str, Any]) -> None:
        if self._shutting_down:
            return
        event_type = event.get("type", "")
        try:
            if event_type == "network":
                self.network = {
                    "listening": bool(event.get("listening")),
                    "port": int(event.get("port", 0) or 0),
                    "host": event.get("host", "::"),
                }
                self._apply_network_state()
            elif event_type == "message_new":
                self._on_message_new(event.get("message") or {})
            elif event_type == "message_updated":
                self._on_message_updated(event.get("message") or {})
            elif event_type == "transfer_progress":
                if self.selected_id and event.get("contact_id") == self.selected_id:
                    self.chat.update_progress(event)
            elif event_type == "file_offer_received":
                self._on_file_offer_received(event.get("offer") or {})
            elif event_type == "file_offer_closed":
                self._on_file_offer_closed(event)
            elif event_type == "file_resume_started":
                name = str(event.get("name", "文件"))
                self._toast(f"正在继续接收 {name}", "info", duration=3200)
            elif event_type == "file_auto_saved":
                name = str(event.get("name", "文件"))
                self._toast(f"已自动保存到下载目录：{name}", "success", duration=3800)
            elif event_type in ("contact_added", "contact_updated"):
                contact = event.get("contact") or {}
                if contact.get("id"):
                    self.contacts[contact["id"]] = contact
                    if contact["id"] not in self.latest:
                        self.latest[contact["id"]] = None
                    if self.selected_id == contact["id"]:
                        self.chat.update_contact(contact)
                    self._refresh_contacts()
                    if event_type == "contact_added" and not self.selected_id:
                        self.select_contact(contact["id"])
            elif event_type == "contact_deleted":
                contact_id = event.get("contact_id", "")
                self.contacts.pop(contact_id, None)
                self.latest.pop(contact_id, None)
                self.unread.pop(contact_id, None)
                self._refresh_contacts()
                if self.selected_id == contact_id:
                    remaining = self._sorted_contacts()
                    if remaining:
                        self.select_contact(remaining[0]["id"])
                    else:
                        self._show_welcome()
            elif event_type == "contact_request":
                self.pending_requests = list(event.get("pending") or [])
                self._refresh_requests()
                request = event.get("request") or {}
                name = request.get("name") or "新联系人"
                if self.isVisible():
                    self._toast(f"{name} 请求添加你为好友", "info")
                    if not self.isActiveWindow():
                        QApplication.alert(self, 2500)
                else:
                    self._notify_tray(APP_NAME, f"{name} 请求添加你为好友")
            elif event_type == "pending_requests_changed":
                self.pending_requests = list(event.get("pending") or [])
                self._refresh_requests()
            elif event_type == "friend_accepted":
                contact = event.get("contact") or {}
                if contact.get("id"):
                    self.contacts[contact["id"]] = contact
                    if self.selected_id == contact.get("id"):
                        self.chat.update_contact(contact)
                    self._refresh_contacts()
                self._toast("对方已接受你的好友请求", "success")
            elif event_type == "profile_updated":
                self.profile = event.get("profile") or self.profile
                self.settings = event.get("settings") or self.settings
                self._refresh_profile()
            elif event_type == "error":
                scope = event.get("scope", "general")
                self._toast(str(event.get("message", "操作失败")), "error", duration=5600)
                if scope == "network":
                    self.network["listening"] = False
                    self._apply_network_state()
            elif event_type == "security_alert":
                message = event.get("message", "对方身份指纹校验失败")
                self._toast(message, "error", duration=7000)
                QApplication.alert(self, 3000)
            elif event_type == "update_offer_received":
                self._on_update_offer_received(event.get("offer") or {})
            elif event_type == "update_progress":
                dialog = self._update_dialogs.get(str(event.get("offer_id", "")))
                if dialog is not None:
                    dialog.set_progress(event)
            elif event_type == "update_downloaded":
                self._on_update_downloaded(event)
            elif event_type == "update_failed":
                self._on_update_failed(event)
            elif event_type == "update_canceled":
                dialog = self._update_dialogs.get(str(event.get("offer_id", "")))
                if dialog is not None:
                    dialog.mark_failed("已取消本次更新")
            elif event_type == "update_invited":
                name = str(event.get("contact_name", "好友"))
                self._toast(
                    f"已向 {name} 推送 {event.get('version', __version__)} 更新，等待对方确认",
                    "info",
                    duration=4200,
                )
            elif event_type == "update_declined":
                name = str(event.get("contact_name", "好友"))
                self._toast(f"{name} 没有接收本次版本更新", "info", duration=3600)
            elif event_type == "update_pushed":
                name = str(event.get("contact_name", "好友"))
                self._toast(f"{name} 已完成更新包接收", "success", duration=4200)
            elif event_type == "attachment_open":
                path = event.get("path", "")
                if path and event.get("opened"):
                    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                    self._toast("文件已临时解密并打开", "info")
                elif path:
                    self._toast("文件已解密导出", "success")
            elif event_type == "toast":
                self._toast(
                    str(event.get("message", "")),
                    str(event.get("level", "info")),
                )
        except Exception:
            log.exception("failed to handle runtime event: %s", event_type)

    # ------------------------------------------------------- file consent UI

    def _on_file_offer_received(self, offer: dict[str, Any]) -> None:
        if not offer.get("file_id"):
            return
        self._file_offer_queue.append(dict(offer))
        contact_name = str(offer.get("contact_name", "好友"))
        name = str(offer.get("name", "文件"))
        self._toast(f"{contact_name} 请求发送 {name}", "info", duration=3600)
        if not self.isActiveWindow():
            QApplication.alert(self, 2500)
        self._show_next_file_offer()

    def _show_next_file_offer(self) -> None:
        if self._shutting_down or self._file_offer_dialog is not None:
            return
        if not self._file_offer_queue:
            return
        offer = self._file_offer_queue.pop(0)
        dialog = FileOfferDialog(self.runtime, offer, self)
        self._file_offer_dialog = dialog
        dialog.finished.connect(
            lambda _result, current=dialog: self._on_file_offer_dialog_finished(current)
        )
        dialog.open()

    def _on_file_offer_dialog_finished(self, dialog: FileOfferDialog) -> None:
        if self._file_offer_dialog is dialog:
            self._file_offer_dialog = None
        QTimer.singleShot(0, self._show_next_file_offer)

    def _on_file_offer_closed(self, event: dict[str, Any]) -> None:
        file_id = str(event.get("file_id", ""))
        if file_id:
            self._file_offer_queue = [
                offer for offer in self._file_offer_queue
                if str(offer.get("file_id", "")) != file_id
            ]
        dialog = self._file_offer_dialog
        if dialog is not None and str(dialog.offer.get("file_id", "")) == file_id:
            self._file_offer_dialog = None
            dialog.force_close(str(event.get("reason", "")))
            QTimer.singleShot(0, self._show_next_file_offer)

    # --------------------------------------------------------- version updates

    def _push_update(self, contact_id: str) -> None:
        contact = self.contacts.get(contact_id) or self.runtime.vault.get_contact(contact_id)
        if not contact:
            self._toast("联系人不存在", "error")
            return
        name = str(contact.get("name", "好友"))
        box = QMessageBox(self)
        box.setWindowTitle("推送版本更新")
        box.setIcon(QMessageBox.Question)
        box.setText(f"把本机 V6Link {__version__} 推送给「{name}」？")
        box.setInformativeText(
            "只有对方版本更旧时才会收到邀请。更新包通过端到端加密通道直接传输，"
            "并用你的身份密钥签名；对方确认、下载并完成校验后，可自行选择重启安装。"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("推送")
        box.button(QMessageBox.No).setText("取消")
        box.setDefaultButton(QMessageBox.Yes)
        if box.exec() != QMessageBox.Yes:
            return
        try:
            self.runtime.push_update(contact_id)
        except Exception as exc:
            self._toast(str(exc), "error")
            return
        self._toast("正在准备更新包，请稍候…", "info", duration=2800)

    def _on_update_offer_received(self, offer: dict[str, Any]) -> None:
        offer_id = str(offer.get("offer_id", ""))
        if not offer_id or offer_id in self._update_dialogs:
            return
        name = str(offer.get("contact_name", "好友"))
        version = str(offer.get("version", ""))
        self._toast(f"{name} 想推送 V6Link {version} 更新", "info", duration=4200)
        if not self.isActiveWindow():
            QApplication.alert(self, 2500)
            self._notify_tray(APP_NAME, f"{name} 想推送 V6Link {version} 更新")
        dialog = UpdateOfferDialog(self.runtime, offer, self)
        self._update_dialogs[offer_id] = dialog
        dialog.install_requested.connect(self._on_update_install_requested)
        dialog.finished.connect(
            lambda _result, current=offer_id: self._update_dialogs.pop(current, None)
        )
        dialog.open()

    def _on_update_downloaded(self, event: dict[str, Any]) -> None:
        dialog = self._update_dialogs.get(str(event.get("offer_id", "")))
        if dialog is not None:
            dialog.mark_downloaded(event)
            return
        name = str(event.get("contact_name", "好友"))
        self._toast(
            f"已接收 {name} 推送的 {event.get('version', '')} 更新，可在好友菜单重新接收",
            "success",
            duration=5200,
        )

    def _on_update_failed(self, event: dict[str, Any]) -> None:
        message = str(event.get("message", "更新失败"))
        dialog = self._update_dialogs.get(str(event.get("offer_id", "")))
        if dialog is not None:
            dialog.mark_failed(message)
            return
        self._toast(f"版本更新失败：{message}", "error", duration=5600)

    def _on_update_install_requested(self, payload: dict[str, Any]) -> None:
        try:
            self.runtime.install_update(
                str(payload.get("path", "")),
                version=str(payload.get("version", "")),
                sha256=str(payload.get("sha256", "")),
                size=int(payload.get("size", 0) or 0),
            )
        except Exception as exc:
            self._toast(str(exc), "error", duration=7000)
            dialog = self._update_dialogs.get(str(payload.get("offer_id", "")))
            if dialog is not None:
                dialog.mark_failed(str(exc))
            return
        for dialog in list(self._update_dialogs.values()):
            with contextlib.suppress(Exception):
                dialog.done(0)
        self._update_dialogs.clear()
        self._toast(
            f"正在重启并更新到 {payload.get('version', '')}，请稍候…",
            "success",
            duration=3000,
        )
        QTimer.singleShot(300, self._restart_for_update)

    def _restart_for_update(self) -> None:
        log.info("restarting for a peer-pushed version update")
        self._shutdown()
        QApplication.quit()

    def _on_message_new(self, message: dict[str, Any]) -> None:
        if not message:
            return
        contact_id = message.get("contact_id", "")
        contact = self.contacts.get(contact_id)
        if contact is None:
            refreshed = self.runtime.vault.get_contact(contact_id)
            if refreshed:
                self.contacts[contact_id] = refreshed
                contact = refreshed
        if contact is None:
            return
        previous_latest = self.latest.get(contact_id)
        timestamp = float(message.get("created_at", 0.0) or 0.0)
        if previous_latest is None or timestamp >= float(previous_latest.get("created_at", 0.0) or 0.0):
            self.latest[contact_id] = message
        active_chat = self.selected_id == contact_id and self.isActiveWindow()
        incoming = message.get("direction") == "in"
        if incoming and not active_chat:
            self.unread[contact_id] = self.unread.get(contact_id, 0) + 1
        if self.selected_id == contact_id:
            self.chat.add_message(message)
        if incoming and not active_chat:
            preview = _message_preview(message, contact)
            sender = contact.get("name", "好友")
            if self.isVisible():
                self._toast(f"{sender}：{preview[:80]}", "info", duration=3400)
                if not self.isActiveWindow():
                    QApplication.alert(self, 2500)
            else:
                self._notify_tray(sender, preview[:160])
        self._refresh_contacts()
        if self.selected_id == contact_id:
            self.setWindowTitle(f"V6Link {__version__} · {contact.get('name', '好友')}")

    def _on_message_updated(self, message: dict[str, Any]) -> None:
        if not message:
            return
        contact_id = message.get("contact_id", "")
        latest = self.latest.get(contact_id)
        if latest and latest.get("id") == message.get("id"):
            self.latest[contact_id] = message
        if self.selected_id == contact_id:
            self.chat.update_message(message)
        if (
            message.get("direction") == "in"
            and message.get("kind") == "file"
            and message.get("status") == "ready"
            and latest is not None
            and latest.get("id") == message.get("id")
        ):
            name = (message.get("file") or {}).get("name", "文件")
            self._toast(f"文件已加密接收：{name}", "success")
        if message.get("direction") == "out" and message.get("status") == "failed":
            self._toast(
                str(message.get("error", "发送失败")),
                "error",
                duration=5200,
            )
        self._refresh_contacts()

    def _toast(self, message: str, level: str = "info", duration: int = 4200) -> None:
        if message and hasattr(self, "toasts"):
            self.toasts.show_message(message, level, duration)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        event_type = event.type()
        if event_type == QEvent.WindowStateChange:
            if self.isMinimized() and not self._shutting_down:
                QTimer.singleShot(0, self._minimized_to_tray)
            return
        if (
            event_type == QEvent.ActivationChange
            and self.isActiveWindow()
            and self.selected_id
            and self.unread.get(self.selected_id)
        ):
            self.unread[self.selected_id] = 0
            self._refresh_contacts()

    # ----------------------------------------------------------------- actions

    def _on_contact_saved(self, contact: dict[str, Any]) -> None:
        if not isinstance(contact, dict) or not contact.get("id"):
            return
        self.contacts[contact["id"]] = contact
        self.latest.setdefault(contact["id"], None)
        self._refresh_contacts()
        self.select_contact(contact["id"])
        self._toast("联系人已保存", "success")

    def _open_add_contact(self) -> None:
        dialog = ContactDialog(self.runtime, parent=self)
        dialog.contact_saved.connect(self._on_contact_saved)
        dialog.exec()
        self._refresh_contacts()

    def _open_contact_edit(self, contact_id: str) -> None:
        contact = self.contacts.get(contact_id) or self.runtime.vault.get_contact(contact_id)
        if not contact:
            self._toast("联系人不存在", "error")
            return
        dialog = ContactDialog(self.runtime, contact=contact, parent=self)
        dialog.contact_saved.connect(self._on_contact_saved)
        dialog.exec()

    def _open_current_menu(self, global_pos: QPoint) -> None:
        if self.selected_id:
            self.show_friend_menu(self.selected_id, global_pos)

    def show_friend_menu(self, contact_id: str, global_pos: QPoint) -> None:
        contact = self.contacts.get(contact_id)
        if contact is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction(icon("edit", C.TEXT_SOFT, 15), "编辑好友与地址")
        update_action = menu.addAction(icon("arrow_down", C.ACCENT, 15), "推送版本更新…")
        if contact.get("peer_id"):
            verify_text = "取消已验证标记" if contact.get("verified") else "标记身份指纹为已验证"
            verify_action = menu.addAction(icon("shield", C.ACCENT, 15), verify_text)
            copy_action = menu.addAction(icon("copy", C.TEXT_SOFT, 15), "复制身份指纹")
            addr = (contact.get("addresses") or [{}])[0]
            copy_addr_action = menu.addAction(icon("copy", C.TEXT_SOFT, 15), "复制首个 IPv6 地址")
        else:
            verify_action = None
            copy_action = None
            copy_addr_action = None
        menu.addSeparator()
        delete_action = menu.addAction(icon("trash", C.DANGER, 15), "删除好友")
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen == edit_action:
            self._open_contact_edit(contact_id)
        elif chosen == update_action:
            self._push_update(contact_id)
        elif verify_action is not None and chosen == verify_action:
            verified = not bool(contact.get("verified"))
            try:
                self.runtime.update_contact(contact_id, {"verified": verified})
            except Exception as exc:
                self._toast(str(exc), "error")
                return
            self._toast(
                "已标记为已验证" if verified else "已取消验证标记",
                "success" if verified else "info",
            )
        elif copy_action is not None and chosen == copy_action:
            QGuiApplication.clipboard().setText(str(contact.get("peer_id", "")))
            self._toast("身份指纹已复制", "success")
        elif copy_addr_action is not None and chosen == copy_addr_action:
            if addr.get("host"):
                QGuiApplication.clipboard().setText(
                    format_endpoint(str(addr.get("host")), int(addr.get("port", 4455)))
                )
                self._toast("IPv6 地址已复制", "success")
        elif chosen == delete_action:
            self._confirm_delete_contact(contact)

    def _confirm_delete_contact(self, contact: dict[str, Any]) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("删除好友")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"确定删除「{contact.get('name', '好友')}」？")
        box.setInformativeText("本机的聊天记录与已接收的加密文件会一并删除，此操作不可撤销。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText("删除")
        box.button(QMessageBox.No).setText("取消")
        box.setDefaultButton(QMessageBox.No)
        if box.exec() == QMessageBox.Yes:
            try:
                self.runtime.delete_contact(contact["id"])
            except Exception as exc:
                self._toast(str(exc), "error")

    def _open_requests(self) -> None:
        self.pending_requests = self.runtime.vault.list_pending_requests()
        self._refresh_requests()
        dialog = PendingRequestsDialog(self.runtime, parent=self)
        dialog.exec()
        self.pending_requests = self.runtime.vault.list_pending_requests()
        self._refresh_requests()
        self._refresh_contacts()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.runtime, parent=self)
        dialog.exec()
        self.profile = self.runtime.vault.profile()
        self.settings = self.runtime.vault.settings()
        snapshot = self.runtime.snapshot()
        self.network = snapshot.get("network", self.network)
        self.peer_id = snapshot.get("peer_id", self.peer_id)
        self._apply_network_state()

    def _open_my_card(self) -> None:
        dialog = MyCardDialog(self.runtime, parent=self)
        dialog.exec()

    def _choose_files(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "选择要加密发送的文件")
        if paths:
            self._send_files(paths)

    def _send_files(self, paths: list[str]) -> None:
        if not self.selected_id:
            self._toast("请先选择一个好友", "warning")
            return
        valid: list[str] = []
        for raw in paths:
            path = Path(str(raw))
            if path.is_dir():
                self._toast("暂不支持直接发送文件夹，请先压缩", "warning")
                continue
            if path.is_file():
                valid.append(str(path))
        if not valid:
            return
        if len(valid) > 20:
            self._toast("一次最多发送 20 个文件", "warning")
            valid = valid[:20]
        scheduled = 0
        for path in valid:
            try:
                self.runtime.send_file(self.selected_id, path)
                scheduled += 1
            except Exception as exc:
                self._toast(str(exc), "error")
        if scheduled:
            self._toast(f"已加入 {scheduled} 个加密文件传输", "info", duration=2600)

    def _send_text(self, text: str) -> None:
        if not self.selected_id:
            return
        clean = str(text or "").strip()
        if not clean:
            return
        try:
            self.runtime.send_text(self.selected_id, text)
        except Exception as exc:
            self.chat.composer.set_text(text)
            self._toast(str(exc), "error")

    def _open_file(self, message_id: str) -> None:
        message = self.runtime.vault.get_message(message_id)
        if message is None:
            self._toast("文件消息不存在", "error")
            return
        file_meta = message.get("file") or {}
        if message.get("direction") == "in":
            if message.get("status") != "ready":
                self._toast("文件尚未接收完成", "warning")
                return
            export_path = str(file_meta.get("export_path", "") or "")
            if export_path and Path(export_path).is_file():
                if QDesktopServices.openUrl(QUrl.fromLocalFile(export_path)):
                    return
            try:
                self.runtime.open_attachment(message_id)
                self._toast("正在校验并临时解密文件…", "info", duration=2600)
            except Exception as exc:
                self._toast(str(exc), "error")
        else:
            source = Path(str(file_meta.get("source_path", "")))
            if not source.is_file():
                self._toast("源文件已被移动或删除", "warning")
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(source)))

    def _reveal_file(self, message_id: str) -> None:
        message = self.runtime.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            self._toast("文件消息不存在", "error")
            return
        file_meta = message.get("file") or {}
        if message.get("direction") == "in":
            raw_path = str(file_meta.get("export_path", "") or "")
            if not raw_path:
                self._toast("请先点「另存为」把文件保存到本地，再打开所在文件夹", "warning")
                return
        else:
            raw_path = str(file_meta.get("source_path", "") or "")
        if not raw_path:
            self._toast("没有可用的本地文件路径", "warning")
            return
        path = Path(raw_path).expanduser()
        if not path.exists():
            self._toast("文件已被移动或删除，无法打开所在文件夹", "warning")
            return
        folder = path if path.is_dir() else path.parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._toast(f"无法打开文件夹：{folder}", "error")
            return
        self._toast("已打开文件所在文件夹", "info", duration=2400)

    def _export_file(self, message_id: str) -> None:
        message = self.runtime.vault.get_message(message_id)
        if message is None:
            self._toast("文件消息不存在", "error")
            return
        if message.get("direction") != "in" or message.get("status") != "ready":
            self._toast("该文件没有可导出的加密副本", "warning")
            return
        file_meta = message.get("file") or {}
        name = str(file_meta.get("name", "file"))
        suggested = Path.home() / "Downloads" / name
        target, _selected = QFileDialog.getSaveFileName(self, "解密并另存为", str(suggested))
        if not target:
            return
        try:
            self.runtime.export_attachment(message_id, target)
            self._toast("正在解密导出…", "info", duration=2600)
        except Exception as exc:
            self._toast(str(exc), "error")

    def _retry_message(self, message_id: str) -> None:
        try:
            self.runtime.retry_message(message_id)
        except Exception as exc:
            self._toast(str(exc), "error")

    def _cancel_transfer(self, message_id: str) -> None:
        message = self.runtime.vault.get_message(message_id)
        if message is None or message.get("kind") != "file":
            self._toast("文件消息不存在", "error")
            return
        incoming = message.get("direction") == "in"
        box = QMessageBox(self)
        box.setWindowTitle("取消接收" if incoming else "取消传输")
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "确定取消接收这个文件？" if incoming else "确定取消发送这个文件？"
        )
        box.setInformativeText(
            "已经接收或发送的加密分片会被删除，且不能续传。"
            "网络波动或关闭应用造成的暂停不需要取消，之后可以继续。"
        )
        cancel_button = box.addButton(
            "取消接收" if incoming else "取消传输",
            QMessageBox.AcceptRole,
        )
        keep_button = box.addButton("继续传输", QMessageBox.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        if box.clickedButton() is not cancel_button:
            return
        try:
            self.runtime.cancel_transfer(message_id)
            self._toast("正在取消传输并删除分片…", "info", duration=2600)
        except Exception as exc:
            self._toast(str(exc), "error")

    # ------------------------------------------------------------------ window

    def closeEvent(self, event: QCloseEvent) -> None:
        self._shutdown()
        event.accept()
        QApplication.quit()

    # --------------------------------------------------------------- system tray

    def _setup_tray(self) -> None:
        if not TrayIcon.is_available():
            return
        tray = TrayIcon(self)
        tray.show_requested.connect(self._restore_from_tray)
        tray.quit_requested.connect(self._quit_from_tray)
        tray.show()
        self.tray = tray

    def _minimized_to_tray(self) -> None:
        if self.isMinimized():
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if self.tray is None or self._shutting_down or not self.isVisible():
            return
        self._restore_maximized = self.isMaximized()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                APP_NAME,
                "V6Link 仍在后台接收消息。双击托盘图标恢复窗口，右键托盘图标可以退出。",
            )

    def _restore_from_tray(self) -> None:
        if self._shutting_down:
            return
        if not self.isVisible() or self.isMinimized():
            if self._restore_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.selected_id:
            QTimer.singleShot(0, self.chat.focus_composer)

    def _notify_tray(self, title: str, message: str) -> None:
        if self.tray is not None and not self.isVisible() and message:
            self.tray.notify(title, message)

    def _quit_from_tray(self) -> None:
        self._shutdown()
        QApplication.quit()

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.tray is not None:
            self.tray.hide()
        with contextlib.suppress(Exception):
            self.bridge.detach()
        self.runtime.mute_events()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.runtime.stop(timeout=8.0)
        except Exception:
            log.debug("runtime stop during shutdown failed", exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()







