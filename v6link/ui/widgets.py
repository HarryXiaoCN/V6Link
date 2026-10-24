"""Custom widgets for the V6Link chat surface."""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..netutil import (
    elapsed_label,
    human_size,
    human_speed,
)
from .icons import avatar_pixmap, icon, logo_pixmap
from .theme import C, refresh_style, set_role

# --------------------------------------------------------------------- helpers


def label(text: str, role: str = "", parent: QWidget | None = None) -> QLabel:
    widget = QLabel(text, parent)
    if role:
        set_role(widget, role)
    return widget


def hline(object_name: str = "divider") -> QFrame:
    line = QFrame()
    line.setObjectName(object_name)
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line


def format_clock(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    dt = datetime.fromtimestamp(float(timestamp))
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%m/%d")
    return dt.strftime("%Y/%m/%d")


def format_bubble_time(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M")


def format_date_separator(timestamp: float) -> str:
    dt = datetime.fromtimestamp(float(timestamp))
    today = datetime.now().date()
    if dt.date() == today:
        return "今天"
    if dt.date() == today - timedelta(days=1):
        return "昨天"
    if dt.year == datetime.now().year:
        return dt.strftime("%m月%d日")
    return dt.strftime("%Y年%m月%d日")


def _is_online(last_seen: float) -> bool:
    return bool(last_seen) and (time.time() - float(last_seen)) < 120


def _message_preview(message: dict[str, Any] | None, contact: dict[str, Any]) -> str:
    if not contact.get("peer_id"):
        return "首次直连后将固定身份指纹"
    if not message:
        return "还没有消息 · 打个招呼吧"
    status = message.get("status")
    if message.get("kind") == "file":
        name = (message.get("file") or {}).get("name", "文件")
        file_status = message.get("status")
        label = {
            "awaiting_consent": "等待确认",
            "transferring": "传输中",
            "receiving": "接收中",
            "paused": "已暂停",
            "canceled": "已取消",
            "declined": "已拒绝",
            "failed": "传输失败",
            "ready": "已接收",
            "sent": "已发送",
        }.get(file_status, "")
        prefix = f"{label} · " if label else "文件 · "
        return prefix + str(name)
    text = str(message.get("text", "")).replace("\n", " ").strip()
    if message.get("direction") == "out" and status in ("failed", "waiting"):
        prefix = "发送失败 · " if status == "failed" else "等待接受 · "
        return prefix + text
    return text


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)
        self._full_text = text
        self.setText(text)
        self.setToolTip(html.escape(text))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._apply_elide()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        metrics = self.fontMetrics()
        available = max(10, self.width() - 2)
        display = metrics.elidedText(self._full_text, Qt.ElideRight, available)
        super().setText(display)
        self.setToolTip(html.escape(self._full_text))


class Avatar(QWidget):
    def __init__(
        self,
        name: str = "",
        identifier: str = "",
        size: int = 44,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._name = name
        self._identifier = identifier
        self._size = size
        self._online = False
        self._show_status = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._pixmap = avatar_pixmap(name, identifier, size)

    def set_identity(self, name: str, identifier: str) -> None:
        if (self._name, self._identifier) == (name, identifier):
            return
        self._name = name
        self._identifier = identifier
        self._pixmap = avatar_pixmap(name, identifier, self._size)
        self.update()

    def set_status(self, online: bool, visible: bool = True) -> None:
        self._online = bool(online)
        self._show_status = bool(visible)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.drawPixmap(QRectF(0, 0, self.width(), self.height()), self._pixmap, QRectF(self._pixmap.rect()))
        if self._show_status:
            diameter = max(9, int(self._size * 0.23))
            margin = max(1, int(self._size * 0.02))
            x = self._size - diameter - margin
            y = self._size - diameter - margin
            painter.setPen(QPen(QColor(C.SIDEBAR), 2))
            painter.setBrush(QColor(C.SUCCESS if self._online else C.MUTED_2))
            painter.drawEllipse(QRectF(x, y, diameter, diameter))
        painter.end()


class IconButton(QToolButton):
    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        parent: QWidget | None = None,
        *,
        color: str = C.MUTED,
        size: int = 20,
        button_size: int = 34,
        object_name: str = "plainIcon",
    ):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setIcon(icon(icon_name, color, size))
        self.setIconSize(QSize(size, size))
        self.setFixedSize(button_size, button_size)
        self.setCursor(Qt.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)


class SearchInput(QLineEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("searchInput")
        self.setPlaceholderText("搜索好友或地址")
        self.setClearButtonEnabled(True)
        search_action = self.addAction(icon("search", C.MUTED_2, 15), QLineEdit.LeadingPosition)
        search_action.setEnabled(False)


class SelfCard(QFrame):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("softCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(64)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        self.avatar = Avatar("我", "self", 38)
        layout.addWidget(self.avatar)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)
        self.name_label = ElidedLabel("我")
        self.name_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #DCE9F1;")
        self.net_label = ElidedLabel("正在启动…")
        self.net_label.setStyleSheet("font-size: 10px; color: #72889A;")
        text_box.addWidget(self.name_label)
        text_box.addWidget(self.net_label)
        layout.addLayout(text_box, 1)
        arrow = QLabel()
        arrow.setPixmap(icon("chevron_right", C.MUTED_2, 14).pixmap(QSize(14, 14)))
        layout.addWidget(arrow)

    def set_profile(self, display_name: str, peer_id: str) -> None:
        self.avatar.set_identity(display_name or "我", peer_id or "self")
        self.name_label.set_full_text(display_name or "我")
        self.setToolTip(f"我的身份指纹\n{peer_id}")

    def set_network(self, listening: bool, port: int) -> None:
        if listening:
            self.net_label.set_full_text(f"IPv6 监听中 · :{port}")
            self.net_label.setStyleSheet("font-size: 10px; color: #58C7B2;")
        else:
            self.net_label.set_full_text("监听未启动 · 点击设置")
            self.net_label.setStyleSheet("font-size: 10px; color: #F3B65C;")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class FriendItem(QFrame):
    clicked = Signal(str)
    context_requested = Signal(str, QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("friendItem")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(70)
        self.contact_id = ""
        self._selected = False
        self._last_seen = 0.0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(11)
        self.avatar = Avatar("", "", 44)
        layout.addWidget(self.avatar)
        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(3)
        self.name_label = ElidedLabel("")
        self.name_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #E5EFF5;")
        self.preview_label = ElidedLabel("")
        self.preview_label.setStyleSheet("font-size: 11px; color: #748A9C;")
        center.addWidget(self.name_label)
        center.addWidget(self.preview_label)
        layout.addLayout(center, 1)
        right = QVBoxLayout()
        right.setContentsMargins(0, 1, 0, 1)
        right.setSpacing(5)
        self.time_label = QLabel("")
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_label.setStyleSheet("font-size: 9px; color: #607789;")
        self.unread_label = QLabel("")
        self.unread_label.setObjectName("unreadBadge")
        self.unread_label.setAlignment(Qt.AlignCenter)
        self.unread_label.hide()
        right.addWidget(self.time_label, 0, Qt.AlignRight)
        right.addWidget(self.unread_label, 0, Qt.AlignRight)
        right.addStretch(1)
        layout.addLayout(right)
        for child in (self.avatar, self.name_label, self.preview_label, self.time_label, self.unread_label):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_data(
        self,
        contact: dict[str, Any],
        preview: str,
        timestamp: float,
        unread: int,
        selected: bool,
    ) -> None:
        self.contact_id = contact.get("id", "")
        self._last_seen = float(contact.get("last_seen", 0.0) or 0.0)
        name = contact.get("name", "未命名好友")
        self.avatar.set_identity(name, self.contact_id or name)
        self.avatar.set_status(_is_online(self._last_seen), True)
        self.name_label.set_full_text(name)
        self.preview_label.set_full_text(preview)
        self.time_label.setText(format_clock(timestamp or self._last_seen))
        if unread > 0:
            self.unread_label.setText("99+" if unread > 99 else str(unread))
            self.unread_label.setMinimumWidth(18)
            self.unread_label.setFixedHeight(18)
            self.unread_label.show()
        else:
            self.unread_label.hide()
        self.set_selected(selected)
        self.setToolTip(html.escape(name))

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        refresh_style(self)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.contact_id:
            self.clicked.emit(self.contact_id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self.contact_id:
            self.context_requested.emit(self.contact_id, event.globalPos())
        event.accept()


class Sidebar(QFrame):
    add_contact_requested = Signal()
    friend_selected = Signal(str)
    requests_clicked = Signal()
    self_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(332)
        self._rows: dict[str, Any] = {}
        self._ordered: list[dict[str, Any]] = []
        self._selected_id = ""
        self._filter = ""
        self._pages: dict[str, tuple[dict[str, Any], FriendItem]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 20, 16, 14)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 0, 0)
        title = label("消息", "h1")
        header.addWidget(title)
        header.addStretch(1)
        self.add_button = IconButton("plus", "添加好友", button_size=34, size=18, color=C.TEXT_SOFT)
        self.add_button.clicked.connect(self.add_contact_requested)
        header.addWidget(self.add_button)
        outer.addLayout(header)

        self.search = SearchInput()
        self.search.textChanged.connect(self._on_search)
        outer.addWidget(self.search)

        self.request_banner = QFrame()
        self.request_banner.setObjectName("requestBanner")
        self.request_banner.setCursor(Qt.PointingHandCursor)
        request_layout = QHBoxLayout(self.request_banner)
        request_layout.setContentsMargins(12, 9, 9, 9)
        request_layout.setSpacing(9)
        request_icon = QLabel()
        request_icon.setPixmap(icon("users", C.ACCENT, 18).pixmap(QSize(18, 18)))
        request_layout.addWidget(request_icon)
        request_text = QVBoxLayout()
        request_text.setContentsMargins(0, 0, 0, 0)
        request_text.setSpacing(0)
        self.request_title = QLabel("好友请求")
        self.request_title.setTextFormat(Qt.PlainText)
        self.request_title.setStyleSheet("font-size: 12px; font-weight: 600; color: #BCEFE5;")
        self.request_sub = QLabel("")
        self.request_sub.setTextFormat(Qt.PlainText)
        self.request_sub.setStyleSheet("font-size: 10px; color: #6FA99E;")
        request_text.addWidget(self.request_title)
        request_text.addWidget(self.request_sub)
        request_layout.addLayout(request_text, 1)
        self.request_view = QPushButton("查看")
        self.request_view.setObjectName("linkButton")
        self.request_view.setCursor(Qt.PointingHandCursor)
        self.request_view.clicked.connect(self.requests_clicked)
        request_layout.addWidget(self.request_view)
        self.request_banner.mouseReleaseEvent = self._banner_clicked  # type: ignore[method-assign]
        self.request_banner.hide()
        outer.addWidget(self.request_banner)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.list_content = QWidget()
        self.list_layout = QVBoxLayout(self.list_content)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(4)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_content)
        outer.addWidget(self.scroll, 1)

        self.empty_box = QWidget()
        empty_layout = QVBoxLayout(self.empty_box)
        empty_layout.setContentsMargins(18, 10, 18, 10)
        empty_layout.setSpacing(8)
        empty_layout.addStretch(1)
        empty_icon = QLabel()
        empty_icon.setPixmap(icon("chat", "#3C5264", 34).pixmap(QSize(34, 34)))
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_icon)
        empty_title = QLabel("还没有好友")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_title.setStyleSheet("font-size: 13px; font-weight: 600; color: #7C91A2;")
        empty_layout.addWidget(empty_title)
        empty_hint = QLabel("点击右上角 + 添加第一个联系人")
        empty_hint.setAlignment(Qt.AlignCenter)
        empty_hint.setWordWrap(True)
        empty_hint.setStyleSheet("font-size: 11px; color: #566C7D;")
        empty_layout.addWidget(empty_hint)
        empty_layout.addStretch(1)
        self.empty_box.hide()
        outer.addWidget(self.empty_box, 1)

        self.self_card = SelfCard()
        self.self_card.clicked.connect(self.self_clicked)
        for child in self.self_card.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)
        outer.addWidget(self.self_card)

    def _banner_clicked(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.requests_clicked.emit()

    def _on_search(self, text: str) -> None:
        self._filter = text.strip().lower()
        self._reflow()

    def set_profile(self, display_name: str, peer_id: str, listening: bool, port: int) -> None:
        self.self_card.set_profile(display_name, peer_id)
        self.self_card.set_network(listening, port)

    def set_requests(self, requests: list[dict[str, Any]]) -> None:
        if not requests:
            self.request_banner.hide()
            return
        request = requests[0]
        extra = f" · 另有 {len(requests) - 1} 个" if len(requests) > 1 else ""
        self.request_title.setText(f"{request.get('name') or '新联系人'} 请求添加你")
        self.request_sub.setText(f"核对身份指纹后接受{extra}")
        self.request_banner.show()

    def set_friends(self, rows: list[dict[str, Any]], selected_id: str) -> None:
        self._ordered = rows
        self._selected_id = selected_id
        current_ids = {row["contact"].get("id") for row in rows}
        for removed in list(self._rows):
            if removed not in current_ids:
                item = self._rows.pop(removed)
                self.list_layout.removeWidget(item)
                item.deleteLater()
        for row in rows:
            contact = row["contact"]
            contact_id = contact.get("id", "")
            item = self._rows.get(contact_id)
            if item is None:
                item = FriendItem()
                item.clicked.connect(self.friend_selected)
                item.context_requested.connect(self._emit_context)
                self._rows[contact_id] = item
            item.set_data(
                contact,
                row.get("preview", ""),
                float(row.get("timestamp", 0.0) or 0.0),
                int(row.get("unread", 0)),
                contact_id == selected_id,
            )
        self._reflow()

    def _emit_context(self, contact_id: str, pos: QPoint) -> None:
        owner = self.window()
        if hasattr(owner, "show_friend_menu"):
            owner.show_friend_menu(contact_id, pos)

    def _reflow(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self.list_content)
        visible_count = 0
        for row in self._ordered:
            contact = row["contact"]
            contact_id = contact.get("id", "")
            item = self._rows.get(contact_id)
            if item is None:
                continue
            haystack = " ".join(
                [
                    str(contact.get("name", "")),
                    str(contact.get("peer_id", "")),
                    " ".join(str(address.get("host", "")) for address in contact.get("addresses", [])),
                ]
            ).lower()
            matches = not self._filter or self._filter in haystack
            item.setVisible(matches)
            item.set_selected(contact_id == self._selected_id)
            if matches:
                self.list_layout.addWidget(item)
                visible_count += 1
        if self._filter and visible_count == 0 and self._ordered:
            no_result = getattr(self, "_no_result", None)
            if no_result is None:
                self._no_result = QLabel("没有匹配的好友")
                self._no_result.setAlignment(Qt.AlignCenter)
                self._no_result.setStyleSheet("color: #5F7587; font-size: 12px; padding: 26px 0;")
            self._no_result.show()
            self.list_layout.addWidget(self._no_result)
        elif hasattr(self, "_no_result"):
            self._no_result.hide()
        self.list_layout.addStretch(1)
        has_friends = bool(self._ordered)
        self.scroll.setVisible(has_friends)
        self.empty_box.setVisible(not has_friends)

    def set_selected(self, contact_id: str) -> None:
        self._selected_id = contact_id
        for item_id, item in self._rows.items():
            item.set_selected(item_id == contact_id)

# ------------------------------------------------------------------ chat header


class StatusPill(QFrame):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("statusPill")
        self.setFixedHeight(24)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 10, 0)
        layout.setSpacing(6)
        self.dot = QLabel()
        self.dot.setFixedSize(6, 6)
        self.dot.setStyleSheet(f"background: {C.ACCENT}; border-radius: 3px;")
        self.text_label = QLabel(text)
        self.text_label.setStyleSheet("font-size: 10px; color: #76B7AB; font-weight: 600;")
        layout.addWidget(self.dot)
        layout.addWidget(self.text_label)

    def set_state(self, text: str, color: str, text_color: str = "#C8D7E2") -> None:
        self.text_label.setText(text)
        self.dot.setStyleSheet(f"background: {color}; border-radius: 3px;")
        self.text_label.setStyleSheet(f"font-size: 10px; color: {text_color}; font-weight: 600;")


class ChatHeader(QFrame):
    more_clicked = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(78)
        self.contact: dict[str, Any] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 14, 22, 12)
        layout.setSpacing(12)
        self.avatar = Avatar("", "", 44)
        layout.addWidget(self.avatar)
        center = QVBoxLayout()
        center.setContentsMargins(0, 1, 0, 1)
        center.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(7)
        self.name_label = ElidedLabel("")
        self.name_label.setStyleSheet("font-size: 15px; font-weight: 700; color: #ECF4F8;")
        self.name_label.setMinimumWidth(60)
        name_row.addWidget(self.name_label, 1)
        self.verified_badge = QLabel()
        self.verified_badge.setPixmap(icon("shield", C.ACCENT, 14).pixmap(QSize(14, 14)))
        self.verified_badge.setToolTip("身份指纹已由你标记为已验证")
        self.verified_badge.hide()
        name_row.addWidget(self.verified_badge)
        name_row.addStretch(1)
        center.addLayout(name_row)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        lock_pix = QLabel()
        lock_pix.setPixmap(icon("lock", "#57AFA3", 11).pixmap(QSize(11, 11)))
        status_row.addWidget(lock_pix)
        self.security_label = QLabel("端到端加密")
        self.security_label.setStyleSheet("font-size: 10px; color: #58B9AA;")
        status_row.addWidget(self.security_label)
        self.sep = QLabel("·")
        self.sep.setStyleSheet("font-size: 10px; color: #41586A;")
        status_row.addWidget(self.sep)
        self.presence_label = QLabel("尚未直连")
        self.presence_label.setStyleSheet("font-size: 10px; color: #6F8495;")
        status_row.addWidget(self.presence_label)
        status_row.addStretch(1)
        center.addLayout(status_row)
        layout.addLayout(center, 1)
        self.network_pill = StatusPill("IPv6 直连")
        layout.addWidget(self.network_pill)
        self.more_button = IconButton("more", "好友设置", button_size=34, size=18, color=C.MUTED)
        self.more_button.clicked.connect(self._emit_more)
        layout.addWidget(self.more_button)

    def _emit_more(self) -> None:
        self.more_clicked.emit(self.more_button.mapToGlobal(QPoint(0, self.more_button.height() + 4)))

    def set_contact(self, contact: dict[str, Any]) -> None:
        self.contact = contact
        name = contact.get("name", "好友")
        self.avatar.set_identity(name, contact.get("id", name))
        self.avatar.set_status(_is_online(float(contact.get("last_seen", 0.0) or 0.0)), True)
        self.name_label.set_full_text(name)
        verified = bool(contact.get("verified"))
        self.verified_badge.setVisible(verified)
        last_seen = float(contact.get("last_seen", 0.0) or 0.0)
        if _is_online(last_seen):
            self.presence_label.setText("当前在线")
            self.presence_label.setStyleSheet("font-size: 10px; color: #58B9AA;")
        else:
            self.presence_label.setText(elapsed_label(last_seen))
            self.presence_label.setStyleSheet("font-size: 10px; color: #6F8495;")
        self.setToolTip(contact.get("peer_id", ""))

    def set_network(self, listening: bool, port: int) -> None:
        if listening:
            self.network_pill.set_state(f"IPv6 · :{port}", C.ACCENT, "#7ACFC0")
        else:
            self.network_pill.set_state("监听未启动", C.WARNING, "#E0B77B")


# --------------------------------------------------------------- message rows


class _MessageRow(QWidget):
    retry_requested = Signal(str)
    open_requested = Signal(str)
    export_requested = Signal(str)
    cancel_requested = Signal(str)
    reveal_requested = Signal(str)

    def __init__(
        self,
        message: dict[str, Any],
        contact: dict[str, Any],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.message = dict(message)
        self.contact = dict(contact)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)


class TextMessageRow(_MessageRow):
    def __init__(self, message: dict[str, Any], contact: dict[str, Any], parent: QWidget | None = None):
        super().__init__(message, contact, parent)
        incoming = message.get("direction") == "in"
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 1, 0, 1)
        outer.setSpacing(8)
        if incoming:
            avatar = Avatar(contact.get("name", ""), contact.get("id", ""), 28)
            wrapper = QVBoxLayout()
            wrapper.setContentsMargins(0, 0, 0, 0)
            wrapper.addWidget(avatar)
            wrapper.addStretch(1)
            outer.addLayout(wrapper)
            outer.setAlignment(avatar, Qt.AlignTop)
        else:
            outer.addStretch(1)

        self.bubble = QFrame()
        self.bubble.setObjectName("messageIn" if incoming else "messageOut")
        self.bubble.setMaximumWidth(540)
        self.bubble.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bubble.customContextMenuRequested.connect(self._show_context_menu)
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(13, 9, 13, 7)
        bubble_layout.setSpacing(4)
        self.text_label = QLabel(str(message.get("text", "")))
        self.text_label.setObjectName("bubbleText")
        self.text_label.setTextFormat(Qt.PlainText)
        self.text_label.setWordWrap(True)
        self.text_label.setMaximumWidth(500)
        self.text_label.setMinimumWidth(self._preferred_text_width(str(message.get("text", ""))))
        self.text_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_label.setCursor(Qt.IBeamCursor)
        self.text_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_label.customContextMenuRequested.connect(self._show_context_menu)
        bubble_layout.addWidget(self.text_label)
        meta = QHBoxLayout()
        meta.setContentsMargins(0, 1, 0, 0)
        meta.setSpacing(6)
        meta.addStretch(1)
        self.time_label = QLabel(format_bubble_time(message.get("created_at")))
        self.time_label.setObjectName("bubbleMetaOut" if not incoming else "bubbleMeta")
        meta.addWidget(self.time_label)
        self.status_label = QLabel()
        self.status_label.setObjectName("bubbleMetaOut")
        meta.addWidget(self.status_label)
        self.retry_button = QPushButton("重试")
        self.retry_button.setObjectName("linkButton")
        self.retry_button.setCursor(Qt.PointingHandCursor)
        self.retry_button.clicked.connect(lambda: self.retry_requested.emit(self.message["id"]))
        meta.addWidget(self.retry_button)
        bubble_layout.addLayout(meta)
        outer.addWidget(self.bubble, 0, Qt.AlignTop)
        if incoming:
            outer.addStretch(1)
        self._update_status()

    @staticmethod
    def _preferred_text_width(text: str) -> int:
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(14)
        metrics = QFontMetrics(font)
        lines = text.splitlines() or [text]
        natural = max(metrics.horizontalAdvance(line) for line in lines)
        return max(76, min(500, natural + 4))

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction(icon("copy", C.TEXT_SOFT, 16), "复制文本")
        chosen = menu.exec(self.text_label.mapToGlobal(pos))
        if chosen == copy_action:
            QGuiApplication.clipboard().setText(str(self.message.get("text", "")))

    def _update_status(self) -> None:
        incoming = self.message.get("direction") == "in"
        status = self.message.get("status")
        error = self.message.get("error", "")
        if incoming:
            self.status_label.hide()
            self.retry_button.hide()
            return
        labels = {
            "sending": "发送中…",
            "sent": "已送达",
            "waiting": "等待验证",
            "failed": "发送失败",
            "received": "已接收",
        }
        self.status_label.setText(labels.get(status, ""))
        self.status_label.show()
        self.status_label.setStyleSheet(
            "font-size: 10px; color: #FF9AA5;" if status == "failed" else "font-size: 10px; color: #73B4A8;"
        )
        self.retry_button.setVisible(status in ("failed", "waiting"))
        self.bubble.setToolTip(html.escape(error) if error else "")

    def update_message(self, message: dict[str, Any]) -> None:
        self.message = dict(message)
        text = str(message.get("text", ""))
        if self.text_label.text() != text:
            self.text_label.setText(text)
        self._update_status()


class FileMessageRow(_MessageRow):
    def __init__(self, message: dict[str, Any], contact: dict[str, Any], parent: QWidget | None = None):
        super().__init__(message, contact, parent)
        self._progress: dict[str, Any] = {}
        self._transferred = int((message.get("file") or {}).get("received_bytes", 0) or 0)
        self._speed = 0.0
        incoming = message.get("direction") == "in"
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(8)
        if incoming:
            avatar = Avatar(contact.get("name", ""), contact.get("id", ""), 28)
            wrapper = QVBoxLayout()
            wrapper.setContentsMargins(0, 0, 0, 0)
            wrapper.addWidget(avatar)
            wrapper.addStretch(1)
            outer.addLayout(wrapper)
        else:
            outer.addStretch(1)

        self.card = QFrame()
        self.card.setObjectName("fileCardIn" if incoming else "fileCardOut")
        self.card.setMaximumWidth(430)
        self.card.setMinimumWidth(320)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(13, 12, 13, 10)
        card_layout.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(11)
        self.icon_tile = QFrame()
        self.icon_tile.setObjectName("fileIcon" if incoming else "fileIconOut")
        self.icon_tile.setFixedSize(46, 48)
        tile_layout = QVBoxLayout(self.icon_tile)
        tile_layout.setContentsMargins(3, 5, 3, 3)
        tile_layout.setSpacing(0)
        file_pix = QLabel()
        file_pix.setAlignment(Qt.AlignCenter)
        file_pix.setPixmap(icon("file", "#86DACA", 21).pixmap(QSize(21, 21)))
        file_pix.setAttribute(Qt.WA_TransparentForMouseEvents)
        tile_layout.addWidget(file_pix)
        self.ext_label = QLabel(self._extension(message))
        self.ext_label.setObjectName("fileType")
        self.ext_label.setAlignment(Qt.AlignCenter)
        self.ext_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        tile_layout.addWidget(self.ext_label)
        top.addWidget(self.icon_tile)
        file_text = QVBoxLayout()
        file_text.setContentsMargins(0, 1, 0, 0)
        file_text.setSpacing(3)
        self.name_label = ElidedLabel(str((message.get("file") or {}).get("name", "文件")))
        self.name_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6F0F5;")
        self.name_label.setMinimumWidth(160)
        self.size_label = QLabel(human_size(int((message.get("file") or {}).get("size", 0) or 0)))
        self.size_label.setStyleSheet("font-size: 10px; color: #7890A2;")
        file_text.addWidget(self.name_label)
        file_text.addWidget(self.size_label)
        top.addLayout(file_text, 1)
        self.action_holder = QHBoxLayout()
        self.action_holder.setContentsMargins(0, 0, 0, 0)
        self.action_holder.setSpacing(4)
        top.addLayout(self.action_holder)
        card_layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setObjectName("transferBar")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        card_layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(6)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 10px; color: #73B4A8;")
        bottom.addWidget(self.status_label, 1)
        self.time_label = QLabel(format_bubble_time(message.get("created_at")))
        self.time_label.setStyleSheet("font-size: 10px; color: #6B8496;")
        bottom.addWidget(self.time_label)
        card_layout.addLayout(bottom)
        outer.addWidget(self.card, 0, Qt.AlignTop)
        if incoming:
            outer.addStretch(1)
        self._rebuild_actions()
        self.update_message(message)

    @staticmethod
    def _extension(message: dict[str, Any]) -> str:
        name = str((message.get("file") or {}).get("name", ""))
        suffix = Path(name).suffix.lstrip(".").upper()
        return suffix[:4] if suffix else "FILE"

    def _clear_actions(self) -> None:
        while self.action_holder.count():
            item = self.action_holder.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _add_action(self, text: str, handler) -> None:
        button = QToolButton()
        button.setObjectName("fileAction")
        button.setText(text)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(handler)
        self.action_holder.addWidget(button)

    def _rebuild_actions(self) -> None:
        self._clear_actions()
        incoming = self.message.get("direction") == "in"
        status = self.message.get("status")
        if incoming and status == "ready":
            self._add_action("打开", lambda: self.open_requested.emit(self.message["id"]))
            self._add_action("另存为", lambda: self.export_requested.emit(self.message["id"]))
            if (self.message.get("file") or {}).get("export_path"):
                self._add_action(
                    "所在文件夹",
                    lambda: self.reveal_requested.emit(self.message["id"]),
                )
        elif not incoming and status == "sent":
            self._add_action("打开", lambda: self.open_requested.emit(self.message["id"]))
            if (self.message.get("file") or {}).get("source_path"):
                self._add_action(
                    "所在文件夹",
                    lambda: self.reveal_requested.emit(self.message["id"]),
                )
        elif status in ("sending", "awaiting_consent", "transferring", "receiving"):
            label = "取消接收" if incoming else "取消"
            self._add_action(
                label,
                lambda: self.cancel_requested.emit(self.message["id"]),
            )
        elif not incoming and status == "paused":
            self._add_action("继续", lambda: self.retry_requested.emit(self.message["id"]))
        elif not incoming and status in ("failed", "declined", "waiting"):
            self._add_action("重试", lambda: self.retry_requested.emit(self.message["id"]))

    def set_progress(self, payload: dict[str, Any]) -> None:
        self._progress = dict(payload)
        self._transferred = int(payload.get("transferred", 0) or 0)
        self._speed = float(payload.get("speed", 0.0) or 0.0)
        self._refresh_progress_ui()

    def _active(self) -> bool:
        return self.message.get("status") in ("sending", "transferring", "receiving", "waiting")

    def _refresh_progress_ui(self) -> None:
        incoming = self.message.get("direction") == "in"
        file_meta = self.message.get("file") or {}
        total = int(file_meta.get("size", 0) or 0)
        status = self.message.get("status")
        active = status in ("sending", "transferring", "receiving")
        if status == "awaiting_consent":
            self.progress.setVisible(False)
            self.status_label.setText("等待对方确认接收…")
            self.status_label.setStyleSheet("font-size: 10px; color: #E0B77B;")
            self.card.setToolTip("对方确认之前不会创建文件，也不会发送文件数据")
        elif status == "sending":
            self.progress.setVisible(True)
            self.progress.setValue(0)
            self.status_label.setText("正在准备加密传输…")
            self.status_label.setStyleSheet("font-size: 10px; color: #73B4A8;")
            self.card.setToolTip("")
        elif active:
            self.progress.setVisible(True)
            percent = int(self._transferred / total * 1000) if total else 0
            self.progress.setValue(max(0, min(1000, percent)))
            verb = "接收中" if incoming else "加密传输中"
            speed = human_speed(self._speed) if self._speed > 1 else ""
            suffix = f" · {speed}" if speed else ""
            label = f"{verb} {percent / 10:.0f}%{suffix}" if total else f"{verb}…"
            self.status_label.setText(label)
            self.status_label.setStyleSheet("font-size: 10px; color: #73B4A8;")
            self.card.setToolTip("")
        elif status == "paused":
            self.progress.setVisible(bool(total and self._transferred > 0))
            if total and self._transferred > 0:
                self.progress.setValue(max(0, min(1000, int(self._transferred / total * 1000))))
            if incoming:
                self.status_label.setText("接收已暂停 · 等待对方继续")
            else:
                self.status_label.setText("传输已暂停 · 可继续")
            self.status_label.setStyleSheet("font-size: 10px; color: #E0B77B;")
            self.card.setToolTip(html.escape(str(self.message.get("error", ""))))
        elif status == "canceled":
            self.progress.setVisible(False)
            self.status_label.setText(
                "已取消接收，文件不会保留" if incoming else "已取消传输，文件不会保留"
            )
            self.status_label.setStyleSheet("font-size: 10px; color: #FF9AA5;")
            self.card.setToolTip(html.escape(str(self.message.get("error", ""))))
        elif status == "declined":
            self.progress.setVisible(False)
            self.status_label.setText("对方拒绝接收")
            self.status_label.setStyleSheet("font-size: 10px; color: #E0B77B;")
            self.card.setToolTip(html.escape(str(self.message.get("error", ""))))
        elif status == "ready":
            self.progress.setValue(1000)
            export_path = str(file_meta.get("export_path", "") or "")
            if export_path:
                self.status_label.setText("已保存到本地文件")
                self.card.setToolTip(html.escape(export_path))
            else:
                self.status_label.setText("已加密保存到本地保险库")
                self.card.setToolTip("自动保存尚未完成；可在文件卡片右上方选择「另存为」")
            self.status_label.setStyleSheet("font-size: 10px; color: #68B7A9;")
        elif status == "sent":
            self.progress.setValue(1000)
            self.status_label.setText("已加密送达")
            self.status_label.setStyleSheet("font-size: 10px; color: #68B7A9;")
            self.card.setToolTip("")
        elif status == "failed":
            self.progress.setVisible(False)
            self.status_label.setText("传输失败")
            self.status_label.setStyleSheet("font-size: 10px; color: #FF9AA5;")
            self.card.setToolTip(html.escape(str(self.message.get("error", ""))))
        elif status == "waiting":
            self.progress.setVisible(False)
            self.status_label.setText("等待好友接受")
            self.status_label.setStyleSheet("font-size: 10px; color: #E0B77B;")
            self.card.setToolTip("对方接受好友请求后会自动重试")
        else:
            self.progress.setVisible(False)
            self.status_label.setText("")

    def update_message(self, message: dict[str, Any]) -> None:
        self.message = dict(message)
        file_meta = message.get("file") or {}
        self.name_label.set_full_text(str(file_meta.get("name", "文件")))
        self.size_label.setText(human_size(int(file_meta.get("size", 0) or 0)))
        self.ext_label.setText(self._extension(message))
        if message.get("status") in ("paused", "receiving", "transferring"):
            try:
                stored_bytes = int(file_meta.get("received_bytes", 0) or 0)
            except (TypeError, ValueError):
                stored_bytes = 0
            self._transferred = max(self._transferred, stored_bytes)
        self._rebuild_actions()
        self._refresh_progress_ui()


class DateSeparator(QWidget):
    def __init__(self, timestamp: float, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 8)
        layout.addStretch(1)
        pill = QFrame()
        pill.setObjectName("datePill")
        pill_layout = QHBoxLayout(pill)
        pill_layout.setContentsMargins(10, 4, 10, 4)
        text = QLabel(format_date_separator(timestamp))
        text.setObjectName("dateText")
        pill_layout.addWidget(text)
        layout.addWidget(pill)
        layout.addStretch(1)


class ConversationEmpty(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 46, 0, 20)
        layout.setSpacing(7)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setPixmap(icon("lock", "#31505A", 30).pixmap(QSize(30, 30)))
        layout.addWidget(icon_label)
        title = QLabel("这段对话已由端到端加密保护")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 12px; font-weight: 600; color: #627D8D;")
        layout.addWidget(title)
        hint = QLabel("发条消息，或把文件拖进来")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("font-size: 10px; color: #465C6C;")
        layout.addWidget(hint)

# ------------------------------------------------------------------- composer


class MessageEdit(QPlainTextEdit):
    submit_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setPlaceholderText("写一条加密消息…")
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(40)
        self.setAcceptDrops(True)
        self.document().contentsChanged.connect(self._fit_height)

    def _fit_height(self) -> None:
        document_height = int(self.document().size().height()) + 10
        target = max(40, min(132, document_height))
        if target != self.height():
            self.setFixedHeight(target)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not (event.modifiers() & Qt.ShiftModifier):
                self.submit_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class Composer(QFrame):
    send_requested = Signal(str)
    attach_requested = Signal()
    text_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("composerCard")
        self.setAcceptDrops(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(7)
        self.attach_button = IconButton("paperclip", "添加文件", button_size=36, size=19, color=C.MUTED)
        self.attach_button.clicked.connect(self.attach_requested)
        layout.addWidget(self.attach_button, 0, Qt.AlignBottom)
        self.edit = MessageEdit()
        self.edit.setAcceptDrops(True)
        layout.addWidget(self.edit, 1)
        self.send_button = QPushButton()
        self.send_button.setObjectName("sendButton")
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.setIcon(icon("send", C.ACCENT_INK, 18))
        self.send_button.setIconSize(QSize(18, 18))
        self.send_button.setFixedSize(40, 40)
        self.send_button.setToolTip("发送 (Enter)")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._request_send)
        layout.addWidget(self.send_button, 0, Qt.AlignBottom)
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.submit_requested.connect(self._request_send)

    def _on_text_changed(self) -> None:
        text = self.edit.toPlainText()
        self.send_button.setEnabled(bool(text.strip()))
        self.text_changed.emit(text)

    def _request_send(self) -> None:
        text = self.edit.toPlainText()
        if not text.strip():
            return
        self.send_requested.emit(text)
        self.edit.clear()
        self.edit.setFocus()

    def set_text(self, text: str) -> None:
        self.edit.blockSignals(True)
        self.edit.setPlainText(text)
        self.edit.blockSignals(False)
        self.edit._fit_height()
        self.send_button.setEnabled(bool(text.strip()))

    def clear(self) -> None:
        self.edit.clear()

    def text(self) -> str:
        return self.edit.toPlainText()

    def focus(self) -> None:
        self.edit.setFocus()


# ----------------------------------------------------------------- drag & drop


class DropOverlay(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setVisible(False)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(6, 12, 17, 220))
        margin = 24
        rect = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)
        painter.setPen(QPen(QColor(61, 223, 192, 170), 1.5, Qt.DashLine))
        painter.setBrush(QColor(18, 40, 36, 120))
        painter.drawRoundedRect(rect, 22, 22)
        icon_pixmap = icon("download", C.ACCENT, 38).pixmap(QSize(76, 76))
        pix_width = 38
        icon_rect = QRectF(
            self.rect().center().x() - pix_width / 2,
            self.rect().center().y() - 64,
            pix_width,
            pix_width,
        )
        painter.drawPixmap(icon_rect, icon_pixmap, QRectF(icon_pixmap.rect()))
        font = QFont(self.font())
        font.setPixelSize(17)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(C.TEXT))
        painter.drawText(
            QRectF(0, self.rect().center().y() + 2, self.width(), 30),
            Qt.AlignCenter,
            "松开，加密发送文件",
        )
        font.setPixelSize(11)
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        painter.setPen(QColor(C.MUTED))
        painter.drawText(
            QRectF(0, self.rect().center().y() + 34, self.width(), 24),
            Qt.AlignCenter,
            "文件会并行直传，并加密保存到对方保险库",
        )
        painter.end()


# -------------------------------------------------------------------- chat page


class ChatPage(QWidget):
    send_text = Signal(str)
    attach_requested = Signal()
    files_dropped = Signal(list)
    more_clicked = Signal(QPoint)
    open_file_requested = Signal(str)
    export_file_requested = Signal(str)
    retry_requested = Signal(str)
    cancel_requested = Signal(str)
    reveal_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setAcceptDrops(True)
        self.contact: dict[str, Any] | None = None
        self._rows: dict[str, _MessageRow] = {}
        self._last_date_key: str | None = None
        self._drag_hide_timer = QTimer(self)
        self._drag_hide_timer.setSingleShot(True)
        self._drag_hide_timer.setInterval(90)
        self._drag_hide_timer.timeout.connect(self._hide_drop_overlay)
        self._drafts: dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = ChatHeader()
        self.header.more_clicked.connect(self.more_clicked)
        root.addWidget(self.header)
        root.addWidget(hline("headerDivider"))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setAcceptDrops(True)
        self.messages_content = QWidget()
        self.messages_content.setAcceptDrops(True)
        self.messages_layout = QVBoxLayout(self.messages_content)
        self.messages_layout.setContentsMargins(26, 6, 26, 12)
        self.messages_layout.setSpacing(5)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_content)
        root.addWidget(self.scroll, 1)
        self.empty_hint: ConversationEmpty | None = None

        compose_holder = QWidget()
        compose_layout = QVBoxLayout(compose_holder)
        compose_layout.setContentsMargins(26, 2, 26, 14)
        compose_layout.setSpacing(6)
        self.composer = Composer()
        self.composer.send_requested.connect(self._on_send)
        self.composer.attach_requested.connect(self.attach_requested)
        self.composer.text_changed.connect(self._on_composer_changed)
        compose_layout.addWidget(self.composer)
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(4, 0, 4, 0)
        hint_row.setSpacing(5)
        lock_icon = QLabel()
        lock_icon.setPixmap(icon("lock", "#42625F", 10).pixmap(QSize(10, 10)))
        hint_row.addWidget(lock_icon)
        hint = QLabel("AES-256-GCM 加密 · IPv6 点对点直传")
        hint.setStyleSheet("font-size: 9px; color: #4F6875;")
        hint_row.addWidget(hint)
        hint_row.addStretch(1)
        keyboard = QLabel("Enter 发送 · Shift+Enter 换行 · 支持拖入文件")
        keyboard.setStyleSheet("font-size: 9px; color: #405663;")
        hint_row.addWidget(keyboard)
        compose_layout.addLayout(hint_row)
        root.addWidget(compose_holder)

        self.drop_overlay = DropOverlay(self)
        self._install_drop_targets()

    def _install_drop_targets(self) -> None:
        targets = [
            self,
            self.scroll,
            self.scroll.viewport(),
            self.messages_content,
            self.composer,
            self.composer.edit,
            self.composer.edit.viewport(),
        ]
        for target in targets:
            target.setAcceptDrops(True)
            target.installEventFilter(self)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(C.BG_SOFT))
        glow = QRadialGradient(self.width() * 0.78, -40.0, max(520.0, self.width() * 0.72))
        glow.setColorAt(0.0, QColor(38, 175, 155, 30))
        glow.setColorAt(0.5, QColor(24, 87, 104, 12))
        glow.setColorAt(1.0, QColor(8, 13, 19, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawRect(self.rect())
        glow2 = QRadialGradient(-120.0, self.height() * 0.9, max(400.0, self.width() * 0.55))
        glow2.setColorAt(0.0, QColor(42, 92, 145, 16))
        glow2.setColorAt(1.0, QColor(8, 13, 19, 0))
        painter.setBrush(glow2)
        painter.drawRect(self.rect())
        painter.setPen(QPen(QColor(255, 255, 255, 5), 1))
        step = 26
        for x in range(step, self.width(), step):
            for y in range(step, self.height(), step):
                painter.drawPoint(x, y)
        painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.drop_overlay.setGeometry(self.rect())

    def _on_send(self, text: str) -> None:
        if self.contact and text.strip():
            self._drafts.pop(self.contact.get("id", ""), None)
            self.send_text.emit(text)

    def _on_composer_changed(self, text: str) -> None:
        if self.contact:
            contact_id = self.contact.get("id", "")
            if text:
                self._drafts[contact_id] = text
            else:
                self._drafts.pop(contact_id, None)

    def set_network(self, listening: bool, port: int) -> None:
        self.header.set_network(listening, port)

    def set_contact(self, contact: dict[str, Any]) -> None:
        old_id = self.contact.get("id") if self.contact else None
        if old_id and old_id != contact.get("id"):
            draft = self.composer.text()
            if draft:
                self._drafts[old_id] = draft
            else:
                self._drafts.pop(old_id, None)
        self.contact = dict(contact)
        self.header.set_contact(contact)
        new_id = contact.get("id", "")
        self.composer.set_text(self._drafts.get(new_id, ""))

    def update_contact(self, contact: dict[str, Any]) -> None:
        if self.contact and self.contact.get("id") == contact.get("id"):
            self.contact = dict(contact)
            self.header.set_contact(contact)

    def _clear_rows(self) -> None:
        while self.messages_layout.count():
            item = self.messages_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        self._last_date_key = None
        self.empty_hint = None

    def load_messages(self, messages: list[dict[str, Any]]) -> None:
        self._clear_rows()
        self.messages_layout.addStretch(1)
        if not messages:
            self.empty_hint = ConversationEmpty()
            self.messages_layout.insertWidget(0, self.empty_hint)
            return
        for message in messages:
            self._append_message(message, scroll=False)
        QTimer.singleShot(30, self.scroll_to_bottom)

    def _append_message(self, message: dict[str, Any], *, scroll: bool = True) -> None:
        message_id = message.get("id", "")
        if not message_id or message_id in self._rows:
            return
        if self.contact is None:
            return
        created_at = float(message.get("created_at", time.time()))
        date_key = datetime.fromtimestamp(created_at).date().isoformat()
        insert_at = max(0, self.messages_layout.count() - 1)
        if date_key != self._last_date_key:
            separator = DateSeparator(created_at)
            self.messages_layout.insertWidget(insert_at, separator)
            insert_at += 1
            self._last_date_key = date_key
        if self.empty_hint is not None:
            self.empty_hint.hide()
        row: _MessageRow
        if message.get("kind") == "file":
            row = FileMessageRow(message, self.contact)
        else:
            row = TextMessageRow(message, self.contact)
        row.retry_requested.connect(self.retry_requested)
        row.open_requested.connect(self.open_file_requested)
        row.export_requested.connect(self.export_file_requested)
        row.cancel_requested.connect(self.cancel_requested)
        row.reveal_requested.connect(self.reveal_requested)
        self.messages_layout.insertWidget(insert_at, row)
        self._rows[message_id] = row
        if scroll:
            QTimer.singleShot(30, self.scroll_to_bottom)

    def add_message(self, message: dict[str, Any]) -> None:
        if self.contact is None or message.get("contact_id") != self.contact.get("id"):
            return
        was_near_bottom = self.near_bottom()
        self._append_message(
            message,
            scroll=was_near_bottom or message.get("direction") == "out",
        )

    def row_for(self, message_id: str) -> _MessageRow | None:
        return self._rows.get(message_id)

    def update_message(self, message: dict[str, Any]) -> None:
        if self.contact is None or message.get("contact_id") != self.contact.get("id"):
            return
        row = self._rows.get(message.get("id", ""))
        if row is not None:
            row.update_message(message)

    def update_progress(self, payload: dict[str, Any]) -> None:
        row = self._rows.get(payload.get("message_id", ""))
        if isinstance(row, FileMessageRow):
            row.set_progress(payload)

    def scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def near_bottom(self) -> bool:
        bar = self.scroll.verticalScrollBar()
        return bar.maximum() - bar.value() < 120

    def focus_composer(self) -> None:
        self.composer.focus()

    # -------------------------------------------------------------- drag events

    def _has_files(self, event) -> bool:
        mime = event.mimeData()
        return bool(mime and mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()))

    def _show_drop_overlay(self) -> None:
        self._drag_hide_timer.stop()
        if self.drop_overlay.geometry() != self.rect():
            self.drop_overlay.setGeometry(self.rect())
        self.drop_overlay.show()
        self.drop_overlay.raise_()

    def _hide_drop_overlay(self) -> None:
        self.drop_overlay.hide()

    def eventFilter(self, watched, event) -> bool:
        event_type = event.type()
        if event_type in (QEvent.DragEnter, QEvent.DragMove):
            if self._has_files(event) and self.contact is not None:
                self._show_drop_overlay()
                event.acceptProposedAction()
                return True
        elif event_type == QEvent.DragLeave:
            self._drag_hide_timer.start()
            return True
        elif event_type == QEvent.Drop:
            if self._has_files(event):
                self._drag_hide_timer.stop()
                self._hide_drop_overlay()
                paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
                paths = [path for path in paths if path and os.path.isfile(path)]
                if paths:
                    self.files_dropped.emit(paths)
                event.acceptProposedAction()
                return True
        return super().eventFilter(watched, event)


# ------------------------------------------------------------------- welcome


class HeroArt(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(144, 144)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center = self.rect().center()
        glow = QRadialGradient(center, 66)
        glow.setColorAt(0.0, QColor(61, 223, 192, 48))
        glow.setColorAt(0.7, QColor(61, 223, 192, 10))
        glow.setColorAt(1.0, QColor(61, 223, 192, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(0, 0, self.width(), self.height())
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(61, 223, 192, 35), 1))
        painter.drawEllipse(QRectF(10, 10, 124, 124))
        painter.setPen(QPen(QColor(61, 223, 192, 20), 1))
        painter.drawEllipse(QRectF(24, 24, 96, 96))
        pix = logo_pixmap(78)
        painter.drawPixmap(
            QRectF(center.x() - 39, center.y() - 39, 78, 78),
            pix,
            QRectF(pix.rect()),
        )
        painter.setPen(QPen(QColor(C.ACCENT), 1.4))
        for x, y, r in ((18, 56, 2.2), (124, 38, 1.7), (116, 112, 2.4), (30, 112, 1.4)):
            painter.drawEllipse(QRectF(x - r, y - r, r * 2, r * 2))
        painter.end()


class FeatureCard(QFrame):
    def __init__(self, icon_name: str, title: str, subtitle: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("welcomeFeature")
        self.setFixedHeight(78)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(10)
        icon_tile = QFrame()
        icon_tile.setFixedSize(34, 34)
        icon_tile.setStyleSheet(
            "background: #122B2A; border: 1px solid #1D423B; border-radius: 10px;"
        )
        tile_layout = QHBoxLayout(icon_tile)
        tile_layout.setContentsMargins(7, 7, 7, 7)
        icon_label = QLabel()
        icon_label.setPixmap(icon(icon_name, C.ACCENT, 19).pixmap(QSize(19, 19)))
        tile_layout.addWidget(icon_label)
        layout.addWidget(icon_tile)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 1, 0, 1)
        text_box.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #CDDBE5;")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setStyleSheet("font-size: 9px; color: #637B8C;")
        text_box.addWidget(title_label)
        text_box.addWidget(subtitle_label)
        layout.addLayout(text_box, 1)


class WelcomePage(QWidget):
    add_contact_requested = Signal()
    card_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("welcomePage")
        self._listening = False
        self._port = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(44, 28, 44, 28)
        outer.addStretch(1)
        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        hero = HeroArt()
        center.addWidget(hero, 0, Qt.AlignHCenter)
        center.addSpacing(10)
        eyebrow = QLabel("PRIVATE BY DESIGN · V6LINK")
        eyebrow.setAlignment(Qt.AlignCenter)
        eyebrow.setStyleSheet("font-size: 9px; color: #43B7A5; font-weight: 700; letter-spacing: 2px;")
        center.addWidget(eyebrow)
        center.addSpacing(10)
        headline = QLabel("一条只属于你们的 IPv6 通道")
        headline.setAlignment(Qt.AlignCenter)
        headline.setStyleSheet("font-size: 27px; font-weight: 700; color: #EEF6FA;")
        center.addWidget(headline)
        center.addSpacing(9)
        subtitle = QLabel(
            "无需中心服务器。身份认证、消息与文件全部端到端加密，\n"
            "好友的 IPv6 地址变化，也不会改变你们之间的加密身份。"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("font-size: 12px; color: #7F95A6; line-height: 1.5;")
        center.addWidget(subtitle)
        center.addSpacing(24)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        buttons.addStretch(1)
        add_button = QPushButton("添加好友")
        add_button.setObjectName("primaryButton")
        add_button.setCursor(Qt.PointingHandCursor)
        add_button.setIcon(icon("plus", C.ACCENT_INK, 16))
        add_button.setIconSize(QSize(16, 16))
        add_button.clicked.connect(self.add_contact_requested)
        buttons.addWidget(add_button)
        card_button = QPushButton("查看我的加密名片")
        card_button.setCursor(Qt.PointingHandCursor)
        card_button.setIcon(icon("card", C.TEXT_SOFT, 16))
        card_button.setIconSize(QSize(16, 16))
        card_button.clicked.connect(self.card_requested)
        buttons.addWidget(card_button)
        buttons.addStretch(1)
        center.addLayout(buttons)
        center.addSpacing(30)
        features = QHBoxLayout()
        features.setContentsMargins(0, 0, 0, 0)
        features.setSpacing(10)
        features.addStretch(1)
        features.addWidget(FeatureCard("shield", "身份固定", "Ed25519 指纹校验"))
        features.addWidget(FeatureCard("network", "并行直传", "多条 IPv6 加密通道"))
        features.addWidget(FeatureCard("lock", "本地加密", "AES-256-GCM 保险库"))
        features.addStretch(1)
        center.addLayout(features)
        outer.addLayout(center)
        outer.addStretch(1)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 4, 0)
        self.footer_label = QLabel("正在初始化安全监听…")
        self.footer_label.setStyleSheet("font-size: 10px; color: #4D6677;")
        footer.addWidget(self.footer_label)
        footer.addStretch(1)
        self.settings_link = QPushButton("网络与加密设置")
        self.settings_link.setObjectName("linkButton")
        self.settings_link.setCursor(Qt.PointingHandCursor)
        self.settings_link.clicked.connect(self.settings_requested)
        footer.addWidget(self.settings_link)
        outer.addLayout(footer)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(C.BG_SOFT))
        glow = QRadialGradient(self.width() * 0.52, self.height() * 0.28, max(500.0, self.width() * 0.6))
        glow.setColorAt(0.0, QColor(35, 132, 121, 25))
        glow.setColorAt(0.55, QColor(24, 63, 83, 10))
        glow.setColorAt(1.0, QColor(8, 13, 19, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawRect(self.rect())
        painter.setPen(QPen(QColor(255, 255, 255, 5), 1))
        step = 30
        for x in range(step, self.width(), step):
            for y in range(step, self.height(), step):
                painter.drawPoint(x, y)
        painter.end()
        super().paintEvent(event)

    def set_network(self, listening: bool, port: int) -> None:
        self._listening = listening
        self._port = port
        if listening:
            self.footer_label.setText(f"IPv6 监听中 · 端口 {port} · 本地保险库已解锁")
        else:
            self.footer_label.setText("IPv6 监听未启动 · 请检查端口与防火墙设置")









