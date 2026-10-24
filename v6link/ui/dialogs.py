"""Modal flows: add/edit contact, request inbox, settings and identity card."""

from __future__ import annotations

import contextlib
import ipaddress
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..netutil import (
    format_endpoint,
    human_size,
    normalize_ipv6_host,
    parse_endpoint,
    shareable_ipv6_addresses,
)
from .icons import icon
from .theme import C
from .widgets import Avatar, IconButton, hline

# --------------------------------------------------------------------- helpers


class FutureWatch(QObject):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, future, parent: QObject | None = None):
        super().__init__(parent)
        self.future = future
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self) -> None:
        if not self.future.done():
            return
        self.timer.stop()
        try:
            result = self.future.result()
        except BaseException as exc:
            self.failed.emit(str(exc).strip() or exc.__class__.__name__)
        else:
            self.succeeded.emit(result)


def normalize_peer_id(text: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", text or "").lower()
    if compact and not re.fullmatch(r"[0-9a-f]{64}", compact):
        raise ValueError("身份指纹应为 64 位十六进制")
    return compact


def parse_share_card(text: str) -> dict[str, Any]:
    """Parse the human-readable card copied from MyCardDialog."""
    if not text or "V6Link" not in text:
        raise ValueError("剪贴板中没有可识别的 V6Link 加密名片")
    name = ""
    peer_id = ""
    addresses: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name_match = re.match(r"^(?:昵称|名字)\s*[:：]\s*(.+)$", line)
        if name_match:
            name = name_match.group(1).strip()
            continue
        peer_match = re.match(r"^身份指纹\s*[:：]\s*(.+)$", line)
        if peer_match:
            peer_id = normalize_peer_id(peer_match.group(1))
            continue
        endpoint_match = re.search(r"(\[[^\]]+\]:\d+)", line)
        if endpoint_match:
            host, port = parse_endpoint(endpoint_match.group(1))
            try:
                parsed_host = ipaddress.IPv6Address(host.split("%", 1)[0])
            except ValueError:
                continue
            # Loopback points at the receiver itself; link-local addresses only
            # make sense with a locally correct interface scope. Neither is
            # portable between machines, including cards from older versions.
            if parsed_host.is_loopback or parsed_host.is_link_local:
                continue
            key = (host.casefold(), port)
            if key not in seen:
                seen.add(key)
                addresses.append(
                    {
                        "host": host,
                        "port": port,
                        "source": "manual",
                        "label": "名片导入",
                    }
                )
    if not addresses:
        raise ValueError("名片中没有找到可跨设备使用的 IPv6 地址（回环和链路本地地址已忽略）")
    return {"name": name, "peer_id": peer_id, "addresses": addresses}


def _section_title(text: str, hint: str = "") -> QVBoxLayout:
    box = QVBoxLayout()
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(2)
    title = QLabel(text)
    title.setObjectName("settingsTitle")
    box.addWidget(title)
    if hint:
        hint_label = QLabel(hint)
        hint_label.setObjectName("settingsHint")
        hint_label.setWordWrap(True)
        box.addWidget(hint_label)
    return box


class BaseDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None, width: int = 540):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        self.setMaximumWidth(max(width, 680))
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setStyleSheet("QDialog { background: #0C141C; }")
        self.error_label = QLabel("")
        self.error_label.setObjectName("errorHint")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def clear_error(self) -> None:
        self.error_label.hide()
        self.error_label.setText("")

    def _footer(self, primary_text: str) -> tuple[QHBoxLayout, QPushButton]:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        self.error_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self.error_label, 1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton(primary_text)
        save.setObjectName("primaryButton")
        save.setCursor(Qt.PointingHandCursor)
        self.primary_button = save
        row.addWidget(save)
        return row, save


# ---------------------------------------------------------------- address editor


class NumberField(QLineEdit):
    valueChanged = Signal(int)

    def __init__(
        self,
        value: int,
        minimum: int,
        maximum: int,
        parent: QWidget | None = None,
        width: int = 84,
    ):
        super().__init__(parent)
        self.minimum = int(minimum)
        self.maximum = int(maximum)
        self.setValidator(QIntValidator(self.minimum, self.maximum, self))
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setFixedWidth(width)
        self.setText(str(max(self.minimum, min(self.maximum, int(value)))))
        self.textChanged.connect(self._notify)

    def _notify(self, text: str) -> None:
        stripped = text.strip()
        if stripped:
            try:
                self.valueChanged.emit(int(stripped))
            except ValueError:
                pass

    def value(self) -> int:
        try:
            value = int(self.text().strip())
        except ValueError:
            value = self.minimum
        return max(self.minimum, min(self.maximum, value))

    def setValue(self, value: int) -> None:
        self.setText(str(max(self.minimum, min(self.maximum, int(value)))))

    def focusOutEvent(self, event) -> None:
        if not self.text().strip():
            self.setText(str(self.minimum))
        else:
            self.setText(str(self.value()))
        super().focusOutEvent(event)


class AddressRow(QFrame):
    remove_requested = Signal(object)

    def __init__(self, host: str = "", port: int = 4455, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("addressRow")
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 6)
        layout.setSpacing(7)
        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText("IPv6 地址，例如 240e:abcd::2")
        self.host_input.setText(host)
        self.host_input.setClearButtonEnabled(False)
        self.port_input = NumberField(port, 1, 65535, width=88)
        self.port_input.setToolTip("监听端口")
        self.remove_button = IconButton("close", "移除地址", button_size=30, size=14, color=C.MUTED_2)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.host_input, 1)
        port_label = QLabel(":")
        port_label.setStyleSheet("color: #53697A; font-size: 14px;")
        layout.addWidget(port_label)
        layout.addWidget(self.port_input)
        layout.addWidget(self.remove_button)

    def value(self) -> dict[str, Any]:
        text = self.host_input.text().strip()
        if not text:
            raise ValueError("请填写 IPv6 地址，或删除空白行")
        port = self.port_input.value()
        if text.startswith("[") and "]:" in text:
            host, port = parse_endpoint(text)
        elif ":" in text and text.count(":") == 1 and text.rsplit(":", 1)[-1].isdigit():
            raise ValueError("IPv6 地址包含端口时请使用 [地址]:端口")
        else:
            host = normalize_ipv6_host(text)
        return {"host": host, "port": port, "source": "manual", "label": "手动添加"}


class AddressEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: list[AddressRow] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.rows_box = QVBoxLayout()
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(7)
        layout.addLayout(self.rows_box)
        add = QPushButton("+  添加 IPv6 地址")
        add.setObjectName("ghostButton")
        add.setCursor(Qt.PointingHandCursor)
        add.setStyleSheet(
            "QPushButton { color: #6FC8BA; text-align: left; padding: 7px 5px; "
            "font-size: 11px; border: none; background: transparent; }"
            "QPushButton:hover { color: #9AF2E3; }"
        )
        add.clicked.connect(self.add_row)
        layout.addWidget(add, 0, Qt.AlignLeft)
        self.default_port = 4455

    def add_row(self, host: str = "", port: int | None = None) -> AddressRow:
        row = AddressRow(host, port or self.default_port)
        row.remove_requested.connect(self.remove_row)
        row.host_input.textChanged.connect(lambda _text: self.changed.emit())
        self._rows.append(row)
        self.rows_box.addWidget(row)
        self._update_remove_buttons()
        self.changed.emit()
        return row

    def remove_row(self, row: AddressRow) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        row.deleteLater()
        self._update_remove_buttons()
        self.changed.emit()

    def _update_remove_buttons(self) -> None:
        for row in self._rows:
            row.remove_button.setEnabled(len(self._rows) > 1)

    def set_addresses(self, addresses: list[dict[str, Any]]) -> None:
        for row in list(self._rows):
            self.rows_box.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        if not addresses:
            self.add_row()
        else:
            for entry in addresses:
                self.add_row(str(entry.get("host", "")), int(entry.get("port", 4455)))
        self._update_remove_buttons()

    def addresses(self) -> list[dict[str, Any]]:
        if not self._rows:
            raise ValueError("至少需要一个 IPv6 地址")
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for row in self._rows:
            value = row.value()
            key = (value["host"].casefold(), value["port"])
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        if not result:
            raise ValueError("请至少填写一个有效的 IPv6 地址")
        return result


# ------------------------------------------------------------------ contact dialog


class ContactDialog(BaseDialog):
    contact_saved = Signal(object)

    def __init__(
        self,
        runtime,
        contact: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ):
        editing = contact is not None
        super().__init__("编辑好友" if editing else "添加好友", parent, width=566)
        self.runtime = runtime
        self.contact = dict(contact) if contact else None
        self._future_watch: FutureWatch | None = None
        self.setMinimumHeight(610 if not editing else 660)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        top.setContentsMargins(1, 0, 1, 0)
        top.setSpacing(13)
        name = (contact or {}).get("name", "新联系人")
        self.avatar = Avatar(name, (contact or {}).get("peer_id", "") or name, 52)
        top.addWidget(self.avatar)
        top_text = QVBoxLayout()
        top_text.setContentsMargins(0, 2, 0, 0)
        top_text.setSpacing(2)
        title = QLabel("编辑加密联系人" if editing else "添加加密联系人")
        title.setStyleSheet("font-size: 19px; font-weight: 700; color: #ECF4F8;")
        subtitle_text = (
            "身份指纹已固定。修改地址不会改变你们的加密身份。"
            if editing
            else "填写对方的 IPv6 地址与身份，建立点对点加密通道。"
        )
        subtitle = QLabel(subtitle_text)
        subtitle.setStyleSheet("font-size: 11px; color: #748A9B;")
        subtitle.setWordWrap(True)
        top_text.addWidget(title)
        top_text.addWidget(subtitle)
        top.addLayout(top_text, 1)
        if not editing:
            paste_btn = QPushButton("粘贴名片")
            paste_btn.setObjectName("softButton")
            paste_btn.setCursor(Qt.PointingHandCursor)
            paste_btn.setIcon(icon("copy", C.TEXT_SOFT, 14))
            paste_btn.setIconSize(QSize(14, 14))
            paste_btn.setToolTip("从剪贴板读取对方的「我的加密名片」")
            paste_btn.clicked.connect(self._paste_share_card)
            top.addWidget(paste_btn, 0, Qt.AlignTop)
        close_btn = IconButton("close", "关闭", button_size=32, size=16, color=C.MUTED_2)
        close_btn.clicked.connect(self.reject)
        top.addWidget(close_btn, 0, Qt.AlignTop)
        root.addLayout(top)

        root.addWidget(hline())

        name_label = QLabel("好友昵称")
        name_label.setObjectName("settingsTitle")
        root.addWidget(name_label)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("给这位联系人起个名字")
        self.name_input.setText((contact or {}).get("name", ""))
        self.name_input.setMaxLength(40)
        self.name_input.textChanged.connect(self._on_name_changed)
        root.addWidget(self.name_input)

        if editing:
            root.addLayout(
                _section_title(
                    "身份指纹",
                    "这是对方的 Ed25519 公钥指纹，用于抵抗地址变化与冒名连接。",
                )
            )
            self.peer_input = None
            identity = QFrame()
            identity.setObjectName("identityCard")
            identity_layout = QVBoxLayout(identity)
            identity_layout.setContentsMargins(12, 10, 10, 10)
            identity_layout.setSpacing(6)
            peer_id = (contact or {}).get("peer_id", "")
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            fp = QLabel(self._format_peer(peer_id) or "首次连接后自动固定")
            fp.setObjectName("mono")
            fp.setWordWrap(True)
            row.addWidget(fp, 1)
            if peer_id:
                copy_btn = IconButton("copy", "复制完整指纹", button_size=28, size=14, color=C.MUTED)
                copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(peer_id))
                row.addWidget(copy_btn)
            identity_layout.addLayout(row)
            self.verified_check = QCheckBox("我已通过其他渠道核对过对方的身份指纹")
            self.verified_check.setChecked(bool((contact or {}).get("verified")))
            self.verified_check.setEnabled(bool(peer_id))
            identity_layout.addWidget(self.verified_check)
            root.addWidget(identity)
        else:
            self.verified_check = None
            root.addLayout(
                _section_title(
                    "身份指纹（可选）",
                    "从对方的「我的加密名片」中复制填写。留空也可；首次握手后会固定实际指纹。",
                )
            )
            self.peer_input = QLineEdit()
            self.peer_input.setObjectName("fingerprintInput")
            self.peer_input.setPlaceholderText("64 位十六进制指纹，可带空格")
            root.addWidget(self.peer_input)

        address_header = QHBoxLayout()
        address_header.setContentsMargins(0, 1, 0, 0)
        address_header.addLayout(
            _section_title("IPv6 地址列表", "会按最近成功连接排序并自动找回变化后的地址。")
        )
        address_header.addStretch(1)
        root.addLayout(address_header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.address_editor = AddressEditor()
        self.address_editor.set_addresses((contact or {}).get("addresses", []))
        self.address_editor.default_port = runtime.listen_port if editing else 4455
        scroll.setWidget(self.address_editor)

        def fit_address_height() -> None:
            rows = max(1, len(self.address_editor._rows))
            height = 48 * rows + 7 * max(0, rows - 1) + 41
            scroll.setFixedHeight(min(178, max(88, height)))

        self.address_editor.changed.connect(fit_address_height)
        fit_address_height()
        QTimer.singleShot(0, fit_address_height)
        root.addWidget(scroll)

        security_note = QLabel(
            "添加后发送首条消息会向对方发出好友请求，对方接受后自动继续。连接建立时双方会进行临时密钥交换，消息与文件均由 AES-256-GCM 加密。"
        )
        security_note.setWordWrap(True)
        security_note.setStyleSheet("font-size: 10px; color: #5E7A86;")
        root.addWidget(security_note)
        root.addWidget(self.error_label)
        footer, self.save_button = self._footer("保存" if editing else "添加好友")
        footer.insertStretch(0)
        root.addLayout(footer)
        self.save_button.clicked.connect(self._save)
        self.name_input.setFocus()

    @staticmethod
    def _format_peer(peer_id: str) -> str:
        if not peer_id:
            return ""
        return " ".join(peer_id[i : i + 4] for i in range(0, len(peer_id), 4))

    def _on_name_changed(self, text: str) -> None:
        self.avatar.set_identity(text or "新联系人", (self.contact or {}).get("peer_id", "") or text)

    def _paste_share_card(self) -> None:
        self.clear_error()
        try:
            card = parse_share_card(QGuiApplication.clipboard().text())
        except ValueError as exc:
            self.show_error(str(exc))
            return
        if card["name"]:
            self.name_input.setText(card["name"])
        if self.peer_input is not None:
            self.peer_input.setText(card["peer_id"])
        self.address_editor.set_addresses(card["addresses"])
        self.address_editor.changed.emit()
        self.name_input.setFocus()

    def _save(self) -> None:
        self.clear_error()
        if self._future_watch is not None:
            return
        try:
            name = self.name_input.text().strip()
            if not name:
                raise ValueError("请填写好友昵称")
            addresses = self.address_editor.addresses()
            if self.contact is None:
                peer_id = normalize_peer_id(self.peer_input.text() if self.peer_input else "")
                future = self.runtime.add_contact(name, peer_id, addresses)
            else:
                changes: dict[str, Any] = {"name": name, "addresses": addresses}
                if self.verified_check is not None:
                    changes["verified"] = self.verified_check.isChecked()
                future = self.runtime.update_contact(self.contact["id"], changes)
        except ValueError as exc:
            self.show_error(str(exc))
            return
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.save_button.setEnabled(False)
        self.save_button.setText("保存中…")
        self._future_watch = FutureWatch(future, self)
        self._future_watch.succeeded.connect(self._on_saved)
        self._future_watch.failed.connect(self._on_save_failed)

    def _on_saved(self, result) -> None:
        self.contact_saved.emit(result)
        self.accept()

    def _on_save_failed(self, message: str) -> None:
        self._future_watch = None
        self.save_button.setEnabled(True)
        self.save_button.setText("保存" if self.contact else "添加好友")
        self.show_error(message)


# ------------------------------------------------------------------- settings


class SettingsDialog(BaseDialog):
    settings_saved = Signal()

    def __init__(self, runtime, parent: QWidget | None = None):
        super().__init__("设置", parent, width=586)
        self.runtime = runtime
        self.profile = runtime.vault.profile()
        self.settings = runtime.vault.settings()
        self.network = runtime.snapshot().get("network", {})
        self._future_watch: FutureWatch | None = None
        self.setMinimumHeight(740)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        top_text = QVBoxLayout()
        top_text.setContentsMargins(0, 0, 0, 0)
        top_text.setSpacing(2)
        title = QLabel("设置与安全")
        title.setStyleSheet("font-size: 19px; font-weight: 700; color: #ECF4F8;")
        subtitle = QLabel("管理监听端口、传输通道和本地加密状态。")
        subtitle.setStyleSheet("font-size: 11px; color: #748A9B;")
        top_text.addWidget(title)
        top_text.addWidget(subtitle)
        top.addLayout(top_text, 1)
        close_btn = IconButton("close", "关闭", button_size=32, size=16, color=C.MUTED_2)
        close_btn.clicked.connect(self.reject)
        top.addWidget(close_btn, 0, Qt.AlignTop)
        root.addLayout(top)
        root.addWidget(hline())

        profile_section = QFrame()
        profile_section.setObjectName("settingsSection")
        profile_layout = QVBoxLayout(profile_section)
        profile_layout.setContentsMargins(15, 13, 15, 15)
        profile_layout.setSpacing(10)
        profile_layout.addLayout(_section_title("个人资料", "好友看到的名字会包含在加密问候中。"))
        self.name_input = QLineEdit(str(self.profile.get("display_name", "")))
        self.name_input.setMaxLength(40)
        self.name_input.setPlaceholderText("你的昵称")
        profile_layout.addWidget(self.name_input)
        root.addWidget(profile_section)

        network_section = QFrame()
        network_section.setObjectName("settingsSection")
        network_layout = QVBoxLayout(network_section)
        network_layout.setContentsMargins(15, 13, 15, 15)
        network_layout.setSpacing(10)
        network_layout.addLayout(_section_title("IPv6 监听", "修改端口会立即重启监听；请放行 Windows 防火墙。"))
        port_row = QHBoxLayout()
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.setSpacing(9)
        port_label = QLabel("监听端口")
        port_label.setFixedWidth(74)
        self.port_input = NumberField(
            int(runtime.listen_port or self.profile.get("listen_port", 4455)),
            1,
            65535,
            width=116,
        )
        port_row.addWidget(port_label)
        port_row.addWidget(self.port_input)
        self.listener_state = QLabel()
        self.listener_state.setStyleSheet("font-size: 10px; color: #6F8998;")
        if self.network.get("listening"):
            self.listener_state.setText(f"当前监听 :{self.network.get('port', 0)}")
        else:
            self.listener_state.setText("当前未监听")
            self.listener_state.setStyleSheet("font-size: 10px; color: #E0B77B;")
        port_row.addWidget(self.listener_state)
        port_row.addStretch(1)
        network_layout.addLayout(port_row)
        root.addWidget(network_section)

        transfer_section = QFrame()
        transfer_section.setObjectName("settingsSection")
        transfer_layout = QVBoxLayout(transfer_section)
        transfer_layout.setContentsMargins(15, 13, 15, 15)
        transfer_layout.setSpacing(10)
        transfer_layout.addLayout(
            _section_title("并行文件传输", "开启多条独立加密连接同时传一个大文件，吞吐优先、无逐块确认。")
        )
        workers_row = QHBoxLayout()
        workers_row.setContentsMargins(0, 0, 0, 0)
        workers_row.setSpacing(9)
        workers_label = QLabel("并行通道")
        workers_label.setFixedWidth(74)
        self.workers_input = NumberField(
            max(1, min(8, int(self.settings.get("parallel_streams", 4)))),
            1,
            8,
            width=76,
        )
        self.workers_hint = QLabel("")
        self.workers_hint.setStyleSheet("font-size: 10px; color: #6F8998;")
        workers_row.addWidget(workers_label)
        workers_row.addWidget(self.workers_input)
        workers_row.addWidget(self.workers_hint)
        workers_row.addStretch(1)
        transfer_layout.addLayout(workers_row)
        self.workers_input.valueChanged.connect(self._update_workers_hint)
        self._update_workers_hint()
        root.addWidget(transfer_section)

        download_section = QFrame()
        download_section.setObjectName("settingsSection")
        download_layout = QVBoxLayout(download_section)
        download_layout.setContentsMargins(15, 13, 15, 15)
        download_layout.setSpacing(9)
        download_layout.addLayout(
            _section_title(
                "下载目录",
                "接收完成的文件会自动解密保存到这里；保险库仍保留加密副本。",
            )
        )
        download_row = QHBoxLayout()
        download_row.setContentsMargins(0, 0, 0, 0)
        download_row.setSpacing(9)
        self.downloads_input = QLineEdit(
            str(self.settings.get("downloads_dir") or (Path.home() / "Downloads"))
        )
        self.downloads_input.setPlaceholderText("默认：用户下载目录")
        download_row.addWidget(self.downloads_input, 1)
        browse_downloads = QPushButton("浏览…")
        browse_downloads.setObjectName("softButton")
        browse_downloads.setCursor(Qt.PointingHandCursor)
        browse_downloads.clicked.connect(self._browse_downloads)
        download_row.addWidget(browse_downloads)
        download_layout.addLayout(download_row)
        root.addWidget(download_section)

        security_section = QFrame()
        security_section.setObjectName("settingsSection")
        security_layout = QVBoxLayout(security_section)
        security_layout.setContentsMargins(15, 13, 15, 15)
        security_layout.setSpacing(8)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        lock_icon = QLabel()
        lock_icon.setPixmap(icon("shield", C.ACCENT, 18).pixmap(QSize(18, 18)))
        header.addWidget(lock_icon)
        sec_title = QLabel("本地数据加密")
        sec_title.setStyleSheet("font-size: 12px; font-weight: 700; color: #A7E8DD;")
        header.addWidget(sec_title)
        header.addStretch(1)
        lock_pill = QLabel("AES-256-GCM")
        lock_pill.setStyleSheet(
            "background: #102A26; border: 1px solid #1C473F; border-radius: 9px; "
            "color: #63C8B8; font-size: 9px; padding: 3px 8px; font-weight: 700;"
        )
        header.addWidget(lock_pill)
        security_layout.addLayout(header)
        security_text = QLabel(
            "好友列表、消息、身份私钥和文件密钥保存在加密保险库中；接收文件在保险库内以加密容器保存，"
            "完成后会按上面设置自动在下载目录生成一份明文副本。Windows 下主密钥由 DPAPI 绑定当前用户保护。"
        )
        security_text.setWordWrap(True)
        security_text.setStyleSheet("font-size: 10px; color: #6D8594;")
        security_layout.addWidget(security_text)
        path_text = QLabel(f"数据目录  {runtime.vault.data_dir}")
        path_text.setObjectName("mono")
        path_text.setWordWrap(True)
        path_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        security_layout.addWidget(path_text)
        self.version_text = QLabel(f"当前版本  V6Link {__version__}")
        self.version_text.setStyleSheet("font-size: 10px; color: #6D8594;")
        security_layout.addWidget(self.version_text)
        root.addWidget(security_section)
        root.addStretch(1)
        root.addWidget(self.error_label)
        footer, self.save_button = self._footer("保存设置")
        footer.insertStretch(0)
        root.addLayout(footer)
        self.save_button.clicked.connect(self._save)

    def _browse_downloads(self) -> None:
        current = self.downloads_input.text().strip() or str(Path.home() / "Downloads")
        folder = QFileDialog.getExistingDirectory(self, "选择下载目录", current)
        if folder:
            self.downloads_input.setText(folder)

    def _update_workers_hint(self) -> None:
        value = self.workers_input.value()
        if value <= 2:
            text = "低占用，适合不稳定网络"
        elif value <= 4:
            text = "平衡，推荐大多数网络"
        else:
            text = "高吞吐，适合低延迟 IPv6 链路"
        self.workers_hint.setText(f"{value} 条连接 · {text}")

    def _save(self) -> None:
        self.clear_error()
        if self._future_watch is not None:
            return
        name = self.name_input.text().strip()
        if not name:
            self.show_error("昵称不能为空")
            return
        self.save_button.setEnabled(False)
        self.save_button.setText("正在应用…")
        future = self.runtime.apply_settings(
            name,
            self.port_input.value(),
            self.workers_input.value(),
            self.downloads_input.text().strip(),
        )
        self._future_watch = FutureWatch(future, self)
        self._future_watch.succeeded.connect(self._on_saved)
        self._future_watch.failed.connect(self._on_failed)

    def _on_saved(self, _result) -> None:
        self.settings_saved.emit()
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._future_watch = None
        self.save_button.setEnabled(True)
        self.save_button.setText("保存设置")
        self.show_error(message)


# -------------------------------------------------------------------- my card


class CopyableRow(QFrame):
    def __init__(self, value: str, hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("softCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 9, 8, 9)
        layout.setSpacing(8)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("mono")
        self.value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value_label.setWordWrap(True)
        text_box.addWidget(self.value_label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setStyleSheet("font-size: 9px; color: #627C8B;")
            text_box.addWidget(hint_label)
        layout.addLayout(text_box, 1)
        copy_btn = IconButton("copy", "复制", button_size=30, size=14, color=C.MUTED)
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(value))
        layout.addWidget(copy_btn, 0, Qt.AlignTop)


class MyCardDialog(BaseDialog):
    def __init__(self, runtime, parent: QWidget | None = None):
        super().__init__("我的加密名片", parent, width=574)
        self.runtime = runtime
        self.profile = runtime.vault.profile()
        self.peer_id = runtime.vault.my_peer_id
        self.port = runtime.listen_port or int(self.profile.get("listen_port", 4455))
        self.network = runtime.snapshot().get("network", {})
        self.addresses = shareable_ipv6_addresses()[:10]
        self.setMinimumHeight(620)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(13)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(13)
        avatar = Avatar(self.profile.get("display_name", "我"), self.peer_id, 54)
        top.addWidget(avatar)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 2, 0, 0)
        title_box.setSpacing(2)
        title = QLabel(self.profile.get("display_name", "我"))
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size: 19px; font-weight: 700; color: #ECF4F8;")
        subtitle = QLabel("把名片发给好友，对方即可添加你的 IPv6 联系人。")
        subtitle.setStyleSheet("font-size: 11px; color: #748A9B;")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)
        close_btn = IconButton("close", "关闭", button_size=32, size=16, color=C.MUTED_2)
        close_btn.clicked.connect(self.accept)
        top.addWidget(close_btn, 0, Qt.AlignTop)
        root.addLayout(top)

        state = QFrame()
        state.setObjectName("identityCard")
        state_layout = QHBoxLayout(state)
        state_layout.setContentsMargins(12, 9, 12, 9)
        state_layout.setSpacing(8)
        state_icon = QLabel()
        state_icon.setPixmap(icon("shield", C.ACCENT, 16).pixmap(QSize(16, 16)))
        state_layout.addWidget(state_icon)
        if self.network.get("listening"):
            state_text = QLabel(f"加密身份已激活 · 正在 IPv6 端口 {self.port} 监听")
            state_text.setStyleSheet("font-size: 10px; color: #7ACFC0;")
        else:
            state_text = QLabel("IPv6 监听未启动 · 好友暂时无法主动连接你")
            state_text.setStyleSheet("font-size: 10px; color: #E0B77B;")
        state_text.setWordWrap(True)
        state_layout.addWidget(state_text, 1)
        root.addWidget(state)

        root.addLayout(_section_title("身份指纹", "这是你的长期公钥指纹，不是私钥，可以安全分享。"))
        root.addWidget(CopyableRow(self.peer_id, "建议好友通过其他渠道核对前 16 位"))

        addresses_header = QHBoxLayout()
        addresses_header.setContentsMargins(0, 2, 0, 0)
        addresses_header.addLayout(
            _section_title(
                "IPv6 地址",
                "好友需要「地址 + 端口」才能主动连接你；::1 和 fe80:: 不会写入名片。",
            )
        )
        addresses_header.addStretch(1)
        self.copy_all_button = QPushButton("复制名片")
        self.copy_all_button.setObjectName("softButton")
        self.copy_all_button.setCursor(Qt.PointingHandCursor)
        self.copy_all_button.setIcon(icon("copy", C.TEXT_SOFT, 14))
        self.copy_all_button.setIconSize(QSize(14, 14))
        self.copy_all_button.clicked.connect(self._copy_card)
        addresses_header.addWidget(self.copy_all_button)
        root.addLayout(addresses_header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(174)
        container = QWidget()
        address_layout = QVBoxLayout(container)
        address_layout.setContentsMargins(0, 0, 0, 0)
        address_layout.setSpacing(6)
        if not self.addresses:
            address_layout.addWidget(
                QLabel("未发现可分享的全局 IPv6 地址。回环地址和 fe80:: 链路本地地址不会写入名片。")
            )
        else:
            for entry in self.addresses:
                endpoint = format_endpoint(entry.host, self.port)
                hint = f"全局 / 可路由 · {entry.interface or '未知接口'}"
                address_layout.addWidget(CopyableRow(endpoint, hint))
        address_layout.addStretch(1)
        scroll.setWidget(container)
        root.addWidget(scroll)

        note = QLabel(
            "提示：对方添加你后，你会在好友请求中看到对方的身份指纹；接受后双方才能互发消息。"
            "首次连接采用首次信任，建议核对指纹。跨设备通讯需要双方都能通过 IPv6 访问到对方。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 10px; color: #5C7482;")
        root.addWidget(note)
        self.status_hint = QLabel("")
        self.status_hint.setStyleSheet("font-size: 10px; color: #63C8B8;")
        root.addWidget(self.status_hint)
        root.addStretch(1)
        close = QPushButton("完成")
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignRight)

    def _copy_card(self) -> None:
        lines = [
            "V6Link 加密名片",
            f"昵称: {self.profile.get('display_name', '我')}",
            f"身份指纹: {self.peer_id}",
            "IPv6 地址:",
        ]
        if self.addresses:
            for entry in self.addresses:
                kind = {"global": "全局"}.get(entry.kind, entry.kind)
                lines.append(f"  {format_endpoint(entry.host, self.port)}  ({kind})")
        else:
            lines.append("  （尚未检测到 IPv6 地址）")
        lines.append(f"监听端口: {self.port}")
        lines.append("在 V6Link 中添加好友，填入昵称、身份指纹与任一 IPv6 地址。")
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.copy_all_button.setText("已复制")
        self.status_hint.setText("加密名片已复制到剪贴板")
        QTimer.singleShot(1800, self._reset_copy_hint)

    def _reset_copy_hint(self) -> None:
        if self.isVisible():
            self.copy_all_button.setText("复制名片")
            self.status_hint.setText("")


# ------------------------------------------------------------ pending requests


class RequestCard(QFrame):
    accepted = Signal(str)
    declined = Signal(str)

    def __init__(self, request: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("contactRequestRow")
        self.request = request
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 11)
        layout.setSpacing(9)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        avatar = Avatar(request.get("name", "?"), request.get("peer_id", ""), 38)
        header.addWidget(avatar)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 1, 0, 0)
        title_box.setSpacing(1)
        title = QLabel(request.get("name", "新联系人"))
        title.setTextFormat(Qt.PlainText)
        title.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6F0F5;")
        subtitle = QLabel(format_endpoint(str(request.get("host", "")), int(request.get("port", 0))))
        subtitle.setObjectName("mono")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        layout.addLayout(header)
        fingerprint = " ".join(
            str(request.get("peer_id", ""))[i : i + 4] for i in range(0, len(str(request.get("peer_id", ""))), 4)
        )
        fp_label = QLabel(fingerprint)
        fp_label.setObjectName("mono")
        fp_label.setWordWrap(True)
        fp_label.setStyleSheet("font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 9px; color: #6B8292;")
        layout.addWidget(fp_label)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addStretch(1)
        ignore = QPushButton("忽略")
        ignore.setObjectName("ghostButton")
        ignore.clicked.connect(lambda: self._choose(False))
        buttons.addWidget(ignore)
        accept = QPushButton("接受")
        accept.setObjectName("primaryButton")
        accept.clicked.connect(lambda: self._choose(True))
        buttons.addWidget(accept)
        self._buttons = (ignore, accept)
        layout.addLayout(buttons)

    def _choose(self, accepted: bool) -> None:
        for button in self._buttons:
            button.setEnabled(False)
        if accepted:
            self.accepted.emit(self.request["id"])
        else:
            self.declined.emit(self.request["id"])


class PendingRequestsDialog(BaseDialog):
    def __init__(self, runtime, parent: QWidget | None = None):
        super().__init__("好友请求", parent, width=566)
        self.runtime = runtime
        self._watchers: list[FutureWatch] = []
        self.setMinimumHeight(420)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(13)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        title = QLabel("好友请求")
        title.setStyleSheet("font-size: 19px; font-weight: 700; color: #ECF4F8;")
        subtitle = QLabel("只有你接受后，对方才能继续向你发送加密消息。")
        subtitle.setStyleSheet("font-size: 11px; color: #748A9B;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        close_btn = IconButton("close", "关闭", button_size=32, size=16, color=C.MUTED_2)
        close_btn.clicked.connect(self.accept)
        header.addWidget(close_btn, 0, Qt.AlignTop)
        root.addLayout(header)
        root.addWidget(hline())
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.list_layout = QVBoxLayout(self.container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(9)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)
        self.empty = QLabel("目前没有待处理的好友请求")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: #657E8D; font-size: 12px; padding: 28px;")
        root.addWidget(self.empty)
        root.addWidget(self.error_label)
        self._refresh(runtime.vault.list_pending_requests())

    def _refresh(self, requests: list[dict[str, Any]]) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.empty.setVisible(not requests)
        self.scroll.setVisible(bool(requests))
        for request in requests:
            card = RequestCard(request)
            card.accepted.connect(lambda request_id: self._handle(request_id, True))
            card.declined.connect(lambda request_id: self._handle(request_id, False))
            self.list_layout.addWidget(card)
        self.list_layout.addStretch(1)

    def _handle(self, request_id: str, accept: bool) -> None:
        future = (
            self.runtime.accept_pending_request(request_id)
            if accept
            else self.runtime.decline_pending_request(request_id)
        )
        watcher = FutureWatch(future, self)
        watcher.succeeded.connect(lambda _result: self._after(request_id))
        watcher.failed.connect(self.show_error)
        self._watchers.append(watcher)

    def _after(self, request_id: str) -> None:
        remaining = [item for item in self.runtime.vault.list_pending_requests() if item.get("id") != request_id]
        self._refresh(remaining)














# --------------------------------------------------------------- file consent


class FileOfferDialog(BaseDialog):
    """Ask the receiver before any file container or data channel is created."""

    def __init__(self, runtime, offer: dict[str, Any], parent: QWidget | None = None):
        super().__init__("文件接收请求", parent, width=520)
        self.runtime = runtime
        self.offer = dict(offer)
        self._decided = False
        self._remaining = max(1, int(self.offer.get("expires_in", 120)) - 2)
        self.setMinimumHeight(330)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        contact_name = str(self.offer.get("contact_name", "好友"))
        avatar = Avatar(contact_name, str(self.offer.get("contact_id", "")), 42)
        top.addWidget(avatar, 0, Qt.AlignTop)
        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(3)
        title = QLabel(f"{contact_name} 想发送文件")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #ECF4F8;")
        subtitle = QLabel("接收后才会开始建立加密传输通道")
        subtitle.setStyleSheet("font-size: 10px; color: #7890A2;")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading, 1)
        root.addLayout(top)
        root.addWidget(hline())

        file_frame = QFrame()
        file_frame.setObjectName("settingsSection")
        file_layout = QHBoxLayout(file_frame)
        file_layout.setContentsMargins(14, 13, 14, 13)
        file_layout.setSpacing(11)
        file_icon = QLabel()
        file_icon.setPixmap(icon("file", "#86DACA", 24).pixmap(QSize(24, 24)))
        file_layout.addWidget(file_icon, 0, Qt.AlignTop)
        file_text = QVBoxLayout()
        file_text.setContentsMargins(0, 0, 0, 0)
        file_text.setSpacing(3)
        file_name = QLabel(str(self.offer.get("name", "文件")))
        file_name.setWordWrap(True)
        file_name.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6F0F5;")
        size = int(self.offer.get("size", 0) or 0)
        received = int(self.offer.get("received_bytes", 0) or 0)
        size_text = human_size(size)
        if received > 0 and size > 0:
            size_text += f" · 已接收 {received / size * 100:.1f}%"
        size_label = QLabel(size_text)
        size_label.setStyleSheet("font-size: 10px; color: #7890A2;")
        file_text.addWidget(file_name)
        file_text.addWidget(size_label)
        file_layout.addLayout(file_text, 1)
        root.addWidget(file_frame)

        note = QLabel(
            "文件会以 AES-256-GCM 加密保存到本地保险库。拒绝不会下载任何内容；"
            "如果对方在传输中使用「取消」，已接收的分片会被删除。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 10px; color: #6D8594;")
        root.addWidget(note)
        self.countdown = QLabel()
        self.countdown.setStyleSheet("font-size: 10px; color: #E0B77B;")
        root.addWidget(self.countdown)
        root.addStretch(1)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(9)
        buttons.addStretch(1)
        reject_button = QPushButton("拒绝")
        reject_button.setCursor(Qt.PointingHandCursor)
        reject_button.clicked.connect(self.reject)
        buttons.addWidget(reject_button)
        accept_button = QPushButton("接收文件")
        accept_button.setObjectName("primaryButton")
        accept_button.setCursor(Qt.PointingHandCursor)
        accept_button.clicked.connect(self._accept)
        buttons.addWidget(accept_button)
        root.addLayout(buttons)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._update_countdown()

    def _update_countdown(self) -> None:
        self.countdown.setText(f"{self._remaining} 秒内未选择将自动拒绝")

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.reject()
            return
        self._update_countdown()

    def _accept(self) -> None:
        if self._decided:
            return
        try:
            self.runtime.accept_file_offer(str(self.offer.get("file_id", "")))
        except Exception as exc:
            self.show_error(str(exc))
            return
        self._decided = True
        self.timer.stop()
        self.accept()

    def reject(self) -> None:
        if not self._decided:
            self._decided = True
            self.timer.stop()
            with contextlib.suppress(Exception):
                self.runtime.reject_file_offer(str(self.offer.get("file_id", "")))
        super().reject()

    def force_close(self, reason: str = "") -> None:
        self._decided = True
        self.timer.stop()
        super().reject()


class UpdateOfferDialog(BaseDialog):
    """Confirm, track and finish a program update offered by a contact.

    The dialog owns four states: ``offer`` (ask), ``downloading`` (pull with
    progress), ``ready`` (verified and staged) and ``error``. It never installs
    anything by itself; the main window performs the swap after the user picks
    "立即重启并更新".
    """

    install_requested = Signal(dict)

    def __init__(self, runtime, offer: dict[str, Any], parent: QWidget | None = None):
        super().__init__("版本更新邀请", parent, width=560)
        self.runtime = runtime
        self.offer = dict(offer)
        self.offer_id = str(self.offer.get("offer_id", ""))
        self._stage = "offer"
        self._installable = False
        self._ready_info: dict[str, Any] = {}
        self.setMinimumHeight(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(14)

        contact_name = str(self.offer.get("contact_name", "好友"))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        avatar = Avatar(contact_name, str(self.offer.get("contact_id", "")), 42)
        top.addWidget(avatar, 0, Qt.AlignTop)
        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(3)
        self.headline = QLabel(f"{contact_name} 想推送新版本")
        self.headline.setStyleSheet("font-size: 16px; font-weight: 700; color: #ECF4F8;")
        self.subtitle = QLabel("更新包使用对方的身份密钥签名，接收后还会再次校验")
        self.subtitle.setStyleSheet("font-size: 10px; color: #7890A2;")
        heading.addWidget(self.headline)
        heading.addWidget(self.subtitle)
        top.addLayout(heading, 1)
        root.addLayout(top)
        root.addWidget(hline())

        version_frame = QFrame()
        version_frame.setObjectName("settingsSection")
        version_layout = QHBoxLayout(version_frame)
        version_layout.setContentsMargins(14, 13, 14, 13)
        version_layout.setSpacing(11)
        update_icon = QLabel()
        update_icon.setPixmap(icon("shield", "#86DACA", 24).pixmap(QSize(24, 24)))
        version_layout.addWidget(update_icon, 0, Qt.AlignTop)
        version_text = QVBoxLayout()
        version_text.setContentsMargins(0, 0, 0, 0)
        version_text.setSpacing(3)
        current = str(self.offer.get("current_version", ""))
        target = str(self.offer.get("version", ""))
        version_label = QLabel(f"{current or '当前版本'}  →  {target or '新版本'}")
        version_label.setWordWrap(True)
        version_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #E6F0F5;")
        size = int(self.offer.get("size", 0) or 0)
        digest = str(self.offer.get("sha256", ""))
        details = f"{human_size(size)} · SHA-256 {digest[:16]}…"
        details_label = QLabel(details)
        details_label.setStyleSheet("font-size: 10px; color: #7890A2;")
        version_text.addWidget(version_label)
        version_text.addWidget(details_label)
        version_layout.addLayout(version_text, 1)
        root.addWidget(version_frame)

        notes = str(self.offer.get("notes", "") or "").strip()
        if notes:
            notes_label = QLabel(notes)
            notes_label.setWordWrap(True)
            notes_label.setStyleSheet(
                "font-size: 10px; color: #9FB4C2; background: #111C25;"
                " border-radius: 6px; padding: 8px;"
            )
            root.addWidget(notes_label)

        self.hint = QLabel(
            "接收只是把更新包下载到本机并校验，不会立即替换程序；"
            "下载完成后会再询问一次是否重启完成更新。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("font-size: 10px; color: #6D8594;")
        root.addWidget(self.hint)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        root.addWidget(self.progress)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("font-size: 10px; color: #8FA8B8;")
        self.progress_label.hide()
        root.addWidget(self.progress_label)

        root.addStretch(1)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(9)
        buttons.addStretch(1)
        self.later_button = QPushButton("稍后再说")
        self.later_button.setCursor(Qt.PointingHandCursor)
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.later_button)
        self.close_button = QPushButton("稍后重启")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        self.close_button.hide()
        buttons.addWidget(self.close_button)
        self.install_button = QPushButton("立即重启并更新")
        self.install_button.setObjectName("primaryButton")
        self.install_button.setCursor(Qt.PointingHandCursor)
        self.install_button.clicked.connect(self._install)
        self.install_button.hide()
        buttons.addWidget(self.install_button)
        self.accept_button = QPushButton("接收并更新")
        self.accept_button.setObjectName("primaryButton")
        self.accept_button.setCursor(Qt.PointingHandCursor)
        self.accept_button.clicked.connect(self._accept)
        buttons.addWidget(self.accept_button)
        root.addLayout(buttons)
        self._sync_buttons()

    # ------------------------------------------------------------------ states

    def _sync_buttons(self) -> None:
        stage = self._stage
        self.later_button.setVisible(stage in ("offer", "downloading"))
        self.later_button.setText("取消接收" if stage == "downloading" else "稍后再说")
        self.accept_button.setVisible(stage == "offer")
        self.accept_button.setEnabled(stage == "offer")
        self.install_button.setVisible(stage == "ready" and self._installable)
        self.close_button.setVisible(stage in ("ready", "error"))

    def _accept(self) -> None:
        if self._stage != "offer":
            return
        try:
            self.runtime.respond_update_offer(self.offer_id, True)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.clear_error()
        self._stage = "downloading"
        self.headline.setText("正在接收更新包")
        self.subtitle.setText("加密通道直连，接收完成后自动校验签名与 SHA-256")
        self.hint.setText("传输期间可以继续使用其他功能；取消会删除已接收的部分。")
        self.progress.setValue(0)
        self.progress.show()
        self.progress_label.setText("准备接收…")
        self.progress_label.show()
        self._sync_buttons()

    def _install(self) -> None:
        if self._stage != "ready" or not self._ready_info:
            return
        self.install_requested.emit(dict(self._ready_info))

    def set_progress(self, event: dict[str, Any]) -> None:
        if self._stage != "downloading":
            return
        percent = float(event.get("percent", 0.0) or 0.0)
        transferred = int(event.get("transferred", 0) or 0)
        total = int(event.get("total", 0) or 0)
        speed = float(event.get("speed", 0.0) or 0.0)
        self.progress.setValue(max(0, min(1000, int(percent * 10))))
        self.progress_label.setText(
            f"{percent:.1f}% · {human_size(transferred)} / {human_size(total)}"
            f" · {human_size(int(speed))}/s"
        )

    def mark_downloaded(self, payload: dict[str, Any]) -> None:
        self._ready_info = dict(payload)
        self._installable = bool(payload.get("installable"))
        self._stage = "ready"
        self.clear_error()
        self.progress.setValue(1000)
        self.progress.show()
        self.progress_label.show()
        self.progress_label.setText("已下载并通过签名与 SHA-256 校验")
        self.headline.setText("更新包已就绪")
        self.subtitle.setText("已保存到本地，等待你确认重启")
        if self._installable:
            self.hint.setText(
                "点击「立即重启并更新」会退出当前程序，自动备份旧版本、替换为新版本并重新启动。"
            )
        else:
            path = str(payload.get("path", ""))
            self.hint.setText(
                "当前以源码方式运行，程序不会自动替换。更新包已保存到：\n" + path
            )
        self._sync_buttons()

    def mark_failed(self, message: str) -> None:
        self._stage = "error"
        self.progress.hide()
        self.progress_label.hide()
        self.headline.setText("更新接收失败")
        self.subtitle.setText(str(self.offer.get("contact_name", "好友")))
        self.show_error(message or "未知原因")
        self._sync_buttons()

    def reject(self) -> None:
        if self._stage == "downloading":
            with contextlib.suppress(Exception):
                self.runtime.cancel_update(self.offer_id)
            self._stage = "error"
            self.progress.hide()
            self.progress_label.hide()
            self.headline.setText("已取消接收")
            self.show_error("已取消本次更新，已接收的部分已删除")
            self._sync_buttons()
            return
        if self._stage == "offer":
            with contextlib.suppress(Exception):
                self.runtime.respond_update_offer(self.offer_id, False)
        super().reject()
